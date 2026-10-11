"""Read accepted original claims without recording or replaying business work."""
from copy import deepcopy
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from test_completion_contract_v2 import _CompletionV2Fixture
import test_completion_publication as fixture

ARCHIVE_MODULE = fixture.SKILL / 'adapters/autopilot_claim_evidence.py'
MODULE = Path(os.environ['GHOST_ALICE_CORE_ROOT']) / '_shared/original_claim_evidence.py'


class RetainedClaimsTest(_CompletionV2Fixture):
    def setUp(self):
        super().setUp()
        self.source['CODEX_THREAD_ID'] = 'session-1'
        skills = self.root / 'managed-skills'
        provider = skills / 'autopilot-mode/adapters/autopilot_claim_evidence.py'
        provider.parent.mkdir(parents=True)
        provider.symlink_to(ARCHIVE_MODULE)
        self.source['GHOST_ALICE_SKILLS_DIR'] = str(skills)

    def test_core_owns_claim_policy_and_ap_exposes_only_archive_reads(self):
        spec = importlib.util.spec_from_file_location('archive_ownership_under_test', ARCHIVE_MODULE)
        provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)
        self.assertTrue(callable(getattr(provider, 'open_claim_archive', None)),
                        'AP must expose read-only archive primitives')
        for name in ('resolve_retained_claims', 'list_retained_claim_candidates', '_resolve_one'):
            self.assertFalse(hasattr(provider, name), 'AP must not own claim admissibility: '+name)
        self.assertTrue(MODULE.is_file(), 'Core must own original claim validity')
        self.accepted()
        spec = importlib.util.spec_from_file_location('core_ownership_under_test', MODULE)
        core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
        for name in ('validate_evidence', 'validate_references', 'original_verification_time'):
            self.assertFalse(hasattr(core, name), 'Claim policy must reuse archive-format validation')
        with provider.open_claim_archive(self.source) as archive:
            with self.assertRaises(sqlite3.DatabaseError):
                archive.connection.execute('DELETE FROM sessions')
            self.assertEqual(archive.connection.total_changes, 0)
        self.assertEqual(core.resolve_retained_claims(self.source, [self.claim],
                         archive_provider=provider)['status'], 'valid')
        self.alter_state(lambda state: state['acceptance_criteria'][0].update(
            met_completion_check_digest='0'*64))
        self.assertEqual(core.resolve_retained_claims(self.source, [self.claim],
                         archive_provider=provider)['status'], 'invalid')

    def test_missing_managed_archive_provider_is_typed_unavailable(self):
        self.accepted()
        self.assertEqual(self.resolve(source=dict(self.source,
            GHOST_ALICE_SKILLS_DIR=str(self.root/'absent-skills')))['status'], 'unavailable')
        self.assertFalse((self.root/'absent-skills').exists())

    def test_unavailable_archive_entrypoint_does_not_invoke_ap_policy(self):
        self.accepted()
        skills = self.root/'legacy-skills'
        provider = skills/'autopilot-mode/adapters/autopilot_claim_evidence.py'
        provider.parent.mkdir(parents=True)
        provider.write_text('def resolve_retained_claims(*args):\n    raise AssertionError("AP policy called")\n')
        source = dict(self.source, GHOST_ALICE_SKILLS_DIR=str(skills))
        self.assertEqual(self.resolve(source=source)['status'], 'unavailable')
        spec = importlib.util.spec_from_file_location('core_missing_archive_under_test', MODULE)
        core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
        self.assertEqual(core.list_retained_claim_candidates(source)['status'], 'unavailable')

    def test_original_proof_parser_is_owned_by_core(self):
        self.proof()
        path = MODULE.with_name('completion_check_validator.py')
        spec = importlib.util.spec_from_file_location('core_proof_owner_under_test', path)
        parser = importlib.util.module_from_spec(spec); spec.loader.exec_module(parser)
        self.assertTrue(callable(getattr(parser, 'parse_original_completion_evidence', None)))
        self.assertEqual(parser.parse_original_completion_evidence(self.completion),
                         fixture.work_items._parse_completion_evidence(self.completion))
        for proof in (self.completion.replace('verdict: pass', 'verdict: fail', 1),
                      self.completion.replace('- unverified: none', '- unverified: pending')):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                parser.parse_original_completion_evidence(proof)
            with self.assertRaises(fixture.work_items.AutopilotStateError):
                fixture.work_items._parse_completion_evidence(proof)

    def test_ap_proof_parser_fails_closed_when_selected_core_is_missing(self):
        self.proof()
        with mock.patch.dict(os.environ, GHOST_ALICE_CORE_ROOT=str(self.root/'missing-core')):
            with self.assertRaises(fixture.work_items.AutopilotStateError):
                fixture.work_items._parse_completion_evidence(self.completion)

    def test_explicit_archive_core_source_overrides_ambient_without_leaking(self):
        self.accepted()
        original = self.resolve()
        spec = importlib.util.spec_from_file_location('explicit_source_claims_under_test', MODULE)
        core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
        with mock.patch.dict(os.environ, GHOST_ALICE_CORE_ROOT=str(self.root/'stale-ambient-core')):
            environment = dict(os.environ)
            self.assertEqual(self.resolve(), original)
            before = self.bytes_snapshot()
            candidates = core.list_retained_claim_candidates(self.source)
            self.assertEqual(candidates['status'], 'candidates')
            self.assertTrue(candidates['claims'])
            self.assertEqual(self.bytes_snapshot(), before)
            # A subsequent legacy call must see its own ambient authority, not
            # an explicit source leaked by successful archive authentication.
            with self.assertRaises(fixture.work_items.AutopilotStateError):
                fixture.work_items._parse_completion_evidence(self.completion)
            self.assertEqual(dict(os.environ), environment)

    def test_archive_policy_source_restores_after_rejection_and_nested_binding(self):
        self.accepted()
        spec = importlib.util.spec_from_file_location('source_restore_archive_under_test', ARCHIVE_MODULE)
        provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)
        with mock.patch.dict(os.environ, GHOST_ALICE_CORE_ROOT=str(self.root/'stale-ambient-core')):
            environment, before = dict(os.environ), self.bytes_snapshot()
            with provider.open_claim_archive(self.source) as archive:
                with mock.patch.object(provider, '_application_records', side_effect=ValueError('rejected')):
                    with self.assertRaisesRegex(ValueError, 'rejected'):
                        archive.accepted_records('report-unit', self.verified_at)
            with self.assertRaises(fixture.work_items.AutopilotStateError):
                fixture.work_items._parse_completion_evidence(self.completion)
            with fixture.work_items._bind_core_policy_source(self.source):
                self.assertTrue(fixture.work_items._parse_completion_evidence(self.completion)['claims'])
                with self.assertRaisesRegex(ValueError, 'nested failure'):
                    with fixture.work_items._bind_core_policy_source(dict(self.source,
                            GHOST_ALICE_CORE_ROOT=str(self.root/'missing-selected-core'))):
                        with self.assertRaises(fixture.work_items.AutopilotStateError):
                            fixture.work_items._parse_completion_evidence(self.completion)
                        raise ValueError('nested failure')
                self.assertTrue(fixture.work_items._parse_completion_evidence(self.completion)['claims'])
            with self.assertRaises(fixture.work_items.AutopilotStateError):
                fixture.work_items._parse_completion_evidence(self.completion)
            self.assertEqual(self.bytes_snapshot(), before)
            self.assertEqual(dict(os.environ), environment)

    def test_explicit_service_source_controls_prepare_publish_and_apply(self):
        with mock.patch.dict(os.environ, GHOST_ALICE_CORE_ROOT=str(self.root/'stale-ambient-core')):
            environment = dict(os.environ)
            self.accepted()
            self.assertEqual(self.resolve()['status'], 'valid')
            self.assertEqual(self.resolve()['claims'][0]['original_verified_at'], self.verified_at)
            with self.assertRaises(fixture.work_items.AutopilotStateError):
                fixture.work_items._parse_completion_evidence(self.completion)
            self.assertEqual(dict(os.environ), environment)

    def test_policy_source_is_isolated_between_concurrent_callers(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        self.proof()
        barrier = Barrier(2)
        def parse(source):
            with fixture.work_items._bind_core_policy_source(source):
                barrier.wait(timeout=5)
                try:
                    fixture.work_items._parse_completion_evidence(self.completion)
                    return 'valid'
                except fixture.work_items.AutopilotStateError:
                    return 'unavailable'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(parse, [self.source, dict(self.source,
                GHOST_ALICE_CORE_ROOT=str(self.root/'missing-thread-core'))]))
        self.assertEqual(results, ['valid', 'unavailable'])

    def test_legacy_publication_keeps_explicit_core_source_with_stale_ambient(self):
        self.source.pop('GHOST_ALICE_COMPLETION_SCHEMA')
        with mock.patch.dict(os.environ, GHOST_ALICE_CORE_ROOT=str(self.root/'stale-ambient-core')):
            environment = dict(os.environ)
            receipt = self.helper.prepare_completion(**self.arguments, schema_version='v1')
            self.proof()
            result = self.helper.publish_completion(receipt_token=receipt['receipt_token'],
                completion_check=self.completion, verified_at=self.verified_at,
                evidence_source=self.evidence_source, source=self.source, schema_version='v1')
            self.assertEqual(result['status'], 'pending-adapter')
            self.assertEqual(result['verified_at'], self.verified_at)
            self.stop()
            self.assertEqual({row['status'] for row in self.read_state()['acceptance_criteria']}, {'met'})
            with self.assertRaises(fixture.work_items.AutopilotStateError):
                fixture.work_items._parse_completion_evidence(self.completion)
            self.assertEqual(dict(os.environ), environment)

    def resolve(self, claims=None, source=None):
        self.assertTrue(MODULE.is_file(), 'Read-only original-claim resolver is missing')
        spec = importlib.util.spec_from_file_location('claims_under_test', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        before = self.bytes_snapshot()
        try:
            result = module.resolve_retained_claims(self.source if source is None else source,
                [self.claim] if claims is None else claims)
        except AttributeError as exc:
            self.fail('Malformed archives must return a typed result: '+str(exc))
        self.assertEqual(self.bytes_snapshot(), before, 'Resolver changed database, committed WAL, evidence, or business files')
        return result

    def bytes_snapshot(self):
        # SQLite SHM read marks are coordination, not authority mutation. Empty
        # WAL sidecars may be created by mode=ro; committed WAL bytes must stay.
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()
                and not p.name.endswith('-shm')
                and not (p.name.endswith('-wal') and p.stat().st_size == 0)}

    def accepted(self, *, dependencies=None, mapping=None):
        receipt = self.prepare()
        self.proof()
        self.published = self.publish(receipt, dependencies=None if mapping is not None else dependencies or [],
            **({'criterion_dependencies': mapping} if mapping is not None else {}))
        applied = self.stop()['completion_recording']['units'][0]
        self.assertEqual(applied['status'], 'applied')
        self.application = applied['application_reference']
        self.claim = dict(statement='Saved report matches input', work_unit_id='report-unit',
            contract_revision=1, criterion_id='report',
            proof_digest='sha256:' + hashlib.sha256(self.completion.encode()).hexdigest())

    def alter_state(self, transform):
        db = self.root / 'intent/ghost-state.sqlite3'
        with closing(sqlite3.connect(db)) as conn, conn:
            row = conn.execute('SELECT state_json FROM sessions WHERE platform=? AND session_id=?',
                               ('codex', 'session-1')).fetchone()
            state = json.loads(row[0]); transform(state)
            conn.execute('UPDATE sessions SET state_json=? WHERE platform=? AND session_id=?',
                         (json.dumps(state), 'codex', 'session-1'))

    def test_owned_applied_original_claim_and_time_survive_new_input(self):
        self.accepted(); self.followup()
        result = self.resolve()
        self.assertEqual(result['status'], 'valid')
        row = result['claims'][0]
        self.assertEqual(row['validity'], 'valid')
        self.assertEqual(row['original_claims'], ['Saved report matches input'])
        self.assertEqual(row['original_verified_at'], self.verified_at)
        self.assertEqual(row['evidence_reference'], self.published['evidence_references'][0])
        self.assertEqual(row['application_reference'], self.application)
        self.assertEqual({k:row[k] for k in self.claim}, self.claim)

    def test_report_does_not_require_current_execution_route(self):
        from response_route_fixture import register_route
        self.accepted()
        register_route(self.core, self.root / 'intent', selected=[], kind='nonexecution', purpose='explanation')
        self.assertEqual(self.resolve()['status'], 'valid')

    def test_archived_observation_survives_log_loss_but_not_target_change(self):
        artifact=self.root/'business-target.txt'; artifact.write_text('original')
        self.accepted(dependencies=[str(artifact)])
        log=self.root/'synthetic-original-observations.txt'
        for mutation in ('changed','deleted'):
            if mutation=='changed': log.write_text('Later unrelated observations')
            else: log.unlink()
            with self.subTest(mutation=mutation):
                result=self.resolve()
                self.assertEqual(result['status'],'valid',result)
                self.assertEqual(result['claims'][0]['original_verified_at'],self.verified_at)
                self.assertEqual(result['claims'][0]['evidence_reference'],self.published['evidence_references'][0])
        artifact.write_text('changed actual target')
        result=self.resolve()
        self.assertEqual(result['status'],'invalid')
        self.assertEqual(result['claims'][0]['reason'],'dependency-changed')

    def test_changed_dependency_rejects_without_invalidating_met(self):
        artifact = self.root/'checked.txt'; artifact.write_text('original')
        self.accepted(dependencies=[str(artifact)])
        artifact.write_text('changed')
        result = self.resolve()
        self.assertEqual(result['status'], 'invalid')
        self.assertEqual(result['claims'][0]['reason'], 'dependency-changed')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'met'})

    def test_only_requested_criterion_dependencies_are_read(self):
        report = self.root/'report.txt'; report.write_text('original')
        other = self.root/'protected.txt'; other.write_text('original')
        mapping = [dict(work_unit_id='report-unit', contract_revision=1, criterion_id=c,
            locators=[str(p)]) for c,p in [('report',report), ('protected',other)]]
        self.accepted(dependencies=[str(report),str(other)], mapping=mapping)
        other.unlink()
        self.assertEqual(self.resolve()['status'], 'valid')
        result = self.resolve([dict(self.claim, criterion_id='protected')])
        self.assertNotEqual(result['status'], 'valid')

    def test_unreadable_dependency_is_unavailable(self):
        artifact = self.root/'checked.txt'; artifact.write_text('original')
        self.accepted(dependencies=[str(artifact)])
        artifact.unlink(); artifact.mkdir()
        self.assertEqual(self.resolve()['status'], 'unavailable')

    def test_missing_database_is_unavailable_without_creation(self):
        self.claim = dict(statement='Unaccepted', work_unit_id='u', contract_revision=1,
                          criterion_id='c', proof_digest='sha256:'+'0'*64)
        source = dict(self.source, GHOST_ALICE_SESSION_INTENT_ROOT=str(self.root/'absent'))
        self.assertEqual(self.resolve(source=source)['status'], 'unavailable')
        self.assertFalse((self.root/'absent').exists())

    def test_missing_or_conflicting_native_authority_is_unavailable(self):
        self.accepted()
        for update in [{'GHOST_ALICE_SESSION_ID':'foreign'}, {'CODEX_THREAD_ID':'foreign'},
                       {'GHOST_ALICE_PLATFORM':'agent-runtime'}, {'GHOST_ALICE_SESSION_INTENT_ROOT':''},
                       {'GHOST_ALICE_SESSION_ID':'../session-1'}, {'GHOST_ALICE_SESSION_INTENT_ROOT':'relative'}]:
            with self.subTest(update=update):
                self.assertEqual(self.resolve(source=dict(self.source, **update))['status'], 'unavailable')

    def test_foreign_exact_session_cannot_borrow_original_application(self):
        self.accepted()
        from response_route_fixture import native_ledger
        rows = deepcopy(self.read_state()['acceptance_criteria'])
        native_ledger(self.root/'intent', self.core, session='foreign', criteria=rows)
        source = dict(self.source, GHOST_ALICE_SESSION_ID='foreign', CODEX_THREAD_ID='foreign')
        self.assertNotEqual(self.resolve(source=source)['status'], 'valid')

    def test_unapplied_publication_is_not_original_evidence_authority(self):
        receipt = self.prepare(); self.proof(); self.publish(receipt)
        self.claim = dict(statement='Saved report matches input', work_unit_id='report-unit',
            contract_revision=1, criterion_id='report',
            proof_digest='sha256:'+hashlib.sha256(self.completion.encode()).hexdigest())
        self.assertEqual(self.resolve()['status'], 'invalid')

    def test_forged_met_row_does_not_replace_application(self):
        receipt = self.prepare(); self.proof(); self.publish(receipt)
        self.claim = dict(statement='Saved report matches input', work_unit_id='report-unit',
            contract_revision=1, criterion_id='report',
            proof_digest='sha256:'+hashlib.sha256(self.completion.encode()).hexdigest())
        def forge(state):
            for row in state['acceptance_criteria']:
                row.update(status='met', met_completion_check_digest=self.claim['proof_digest'])
        self.alter_state(forge)
        self.assertEqual(self.resolve()['status'], 'invalid')

    def test_current_proof_conflict_rejects(self):
        self.accepted()
        self.alter_state(lambda s:s['acceptance_criteria'][0].update(met_completion_check_digest='f'*64))
        self.assertEqual(self.resolve()['status'], 'invalid')

    def test_changed_current_definition_rejects(self):
        self.accepted()
        self.alter_state(lambda s:s['acceptance_criteria'][0].update(summary='Entire project verified'))
        self.assertEqual(self.resolve()['status'], 'invalid')

    def test_unrelated_context_preserves_original_report_without_republication(self):
        artifact = self.root/'checked.txt'; artifact.write_text('original')
        self.accepted(dependencies=[str(artifact)])
        original_time = self.verified_at
        self.change_intent({'constraints':['Use Markdown for later release notes'],
            'decisions':[{'id':'later-release', 'summary':'Discuss a different release next'}]}, new_input=True)
        result = self.resolve()
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(result['claims'][0]['original_verified_at'], original_time)
        self.assertEqual(result['claims'][0]['application_reference'], self.application)
        artifact.write_text('changed original artifact')
        self.assertEqual(self.resolve()['status'], 'invalid')

    def test_tampered_application_or_evidence_or_origin_is_invalid(self):
        self.accepted()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            evidence = store.completion_read('evidence',self.published['evidence_references'][0])
            origin = evidence['origin_token']
        db = self.root/'intent/ghost-state.sqlite3'
        for kind,ref in [('application',self.application), ('evidence',self.published['evidence_references'][0]), ('origin',origin)]:
            with self.subTest(kind=kind), closing(sqlite3.connect(db)) as conn, conn:
                raw = conn.execute('SELECT body_json FROM ap_completion_v2_records WHERE kind=? AND identity=?',(kind,ref)).fetchone()[0]
                value=json.loads(raw);value['tampered']=True
                conn.execute('UPDATE ap_completion_v2_records SET body_json=? WHERE kind=? AND identity=?',(json.dumps(value),kind,ref));conn.commit()
                self.assertEqual(self.resolve()['status'], 'invalid')
                conn.execute('UPDATE ap_completion_v2_records SET body_json=? WHERE kind=? AND identity=?',(raw,kind,ref));conn.commit()

    def test_unaccepted_application_pointer_is_invalid(self):
        self.accepted()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            state=store.completion_unit('report-unit');state['applied_requests']=[]
            store.completion_set_unit(state)
        self.assertEqual(self.resolve()['status'], 'invalid')

    def test_partial_proof_returns_only_requested_supported_claims(self):
        receipt = self.prepare(); self.proof()
        self.completion = self.completion.replace('[completion-check]', '[partial-check]\n- scope: partial')
        self.completion = self.completion.replace('original bytes match\n    verdict: pass', 'bytes differ\n    verdict: fail')
        self.completion = self.completion.replace('- unverified: none', '- unverified:\n  - protected: Unresolved original condition')
        self.published = self.publish(receipt)
        self.stop()
        self.claim = dict(statement='Saved report matches input', work_unit_id='report-unit',
            contract_revision=1, criterion_id='report',
            proof_digest='sha256:'+hashlib.sha256(self.completion.encode()).hexdigest())
        result = self.resolve()
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(result['claims'][0]['original_claims'], ['Saved report matches input'])
        self.assertEqual(self.resolve([dict(self.claim, criterion_id='protected')])['status'], 'invalid')

    def test_active_wal_observes_current_committed_state_without_writes(self):
        self.accepted()
        with closing(sqlite3.connect(self.root/'intent/ghost-state.sqlite3')) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('CREATE TABLE wal_probe(value TEXT)')
            writer.commit()
            self.assertTrue((self.root/'intent/ghost-state.sqlite3-wal').stat().st_size)
            self.assertEqual(self.resolve()['status'], 'valid')
            raw = writer.execute('SELECT state_json FROM sessions WHERE platform=? AND session_id=?',
                                 ('codex', 'session-1')).fetchone()[0]
            state = json.loads(raw)
            state['acceptance_criteria'][0]['met_completion_check_digest'] = 'f'*64
            writer.execute('UPDATE sessions SET state_json=? WHERE platform=? AND session_id=?',
                           (json.dumps(state),'codex','session-1'))
            writer.commit()
            self.assertEqual(self.resolve()['status'], 'invalid')

    def test_changed_contract_revision_does_not_report_old_qualified_claim(self):
        self.accepted()
        rows = deepcopy(self.read_state()['acceptance_criteria'])
        rows[0]['summary'] = 'New report acceptance condition'
        self.change_intent({'acceptance_criteria':rows}, new_input=True)
        self.assertEqual(self.resolve()['status'], 'invalid')

    def test_candidates_preserve_exact_original_claims_without_dependency_read(self):
        artifact = self.root/'checked.txt'; artifact.write_text('original')
        self.accepted(dependencies=[str(artifact)])
        artifact.unlink(); artifact.mkdir()
        spec = importlib.util.spec_from_file_location('claim_candidates_under_test', MODULE)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(callable(getattr(module, 'list_retained_claim_candidates', None)),
                        'Read-only accepted claim candidate discovery is missing')
        before = self.bytes_snapshot()
        result = module.list_retained_claim_candidates(self.source, limit=1)
        self.assertEqual(self.bytes_snapshot(), before)
        self.assertEqual(result['status'], 'candidates')
        self.assertEqual(len(result['claims']), 1)
        row = result['claims'][0]
        self.assertEqual(row['validity'], 'candidate')
        self.assertIn(row['statement'], ['Saved report matches input', 'Protected file is unchanged'])
        self.assertEqual(row['original_verified_at'], self.verified_at)
        self.assertEqual(row['proof_digest'], self.claim['proof_digest'])
        self.assertEqual(self.resolve([dict(self.claim, statement=row['statement'], criterion_id=row['criterion_id'])])['status'], 'unavailable')

    def test_candidate_limit_prioritizes_recent_applied_unit(self):
        self.accepted()
        rows = deepcopy(self.read_state()['acceptance_criteria'])
        rows.append(dict(id='newer', summary='Newer report verified', source='user-explicit',
                         admitted=True, status='unmet', work_unit_id='newer-unit', contract_revision=1))
        self.core.record_turn(root=self.root/'intent', platform='codex', session_id='session-1',
            raw_user_input='Verify a newer independent deliverable',
            intent_delta={'acceptance_criteria':rows},
            expected_input_event_id=self.read_state()['latest_input_event_id'])
        from response_route_fixture import register_route
        register_route(self.core,self.root/'intent',selected=['newer-unit'])
        self.arguments['input_event_id'] = self.read_state()['latest_input_event_id']
        receipt = self.prepare(); self.proof()
        self.completion = self.completion.replace('report: Saved report matches input','newer: Newer report verified')
        self.completion = self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion = self.completion.replace('Saved report matches input\n    criterion: report','Newer report verified\n    criterion: newer')
        start = self.completion.index('  - claim: Protected file is unchanged')
        end = self.completion.index('- unverified: none')
        self.completion = self.completion[:start]+self.completion[end:]
        self.publish(receipt); self.stop()
        spec = importlib.util.spec_from_file_location('claim_order_under_test', MODULE)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        before = self.bytes_snapshot()
        result = module.list_retained_claim_candidates(self.source, limit=1)
        self.assertEqual(self.bytes_snapshot(), before)
        self.assertEqual(result['status'], 'candidates')
        self.assertEqual(result['claims'][0]['work_unit_id'], 'newer-unit')
        self.assertEqual(result['claims'][0]['statement'], 'Newer report verified')

    def test_foreign_session_with_same_unit_name_does_not_poison_owned_proof(self):
        self.accepted()
        from response_route_fixture import native_ledger
        rows = deepcopy(self.read_state()['acceptance_criteria'])
        for row in rows:
            row['status'] = 'unmet'
            row.pop('met_completion_check_digest', None)
            row.pop('met_at', None)
        native_ledger(self.root/'intent', self.core, session='session-2', criteria=rows)
        other_source = dict(self.source, GHOST_ALICE_SESSION_ID='session-2', CODEX_THREAD_ID='session-2')
        other_arguments = dict(self.arguments, session_id='session-2', source=other_source)
        receipt = self.helper.prepare_completion(**other_arguments, schema_version='v2')
        original_proof, original_time = self.completion, self.verified_at
        self.proof()
        self.review(source=other_source)
        self.helper.publish_completion(receipt_token=receipt['receipt_token'], completion_check=self.completion,
            verified_at=self.verified_at, evidence_source=self.evidence_source, source=other_source, schema_version='v2')
        stopped = fixture.adapter.adapter_payload_from_env(other_source,
            hook_input={'hook_event_name':'Stop', 'session_id':'session-2', 'cwd':str(self.root)})
        self.assertEqual(stopped['completion_recording']['units'][0]['status'], 'applied')
        self.completion, self.verified_at = original_proof, original_time
        result = self.resolve()
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(result['claims'][0]['application_reference'], self.application)

    def test_malformed_mutable_unit_records_fail_closed_for_both_read_apis(self):
        self.accepted()
        spec = importlib.util.spec_from_file_location('malformed_claims_under_test', MODULE)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            original = store.completion_unit('report-unit')
        for field, bad in [('application_references',[]), ('application_references','bad'),
                           ('publications',[]), ('publications',None), ('applied_requests','bad')]:
            with self.subTest(field=field, bad=bad):
                with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
                    store.completion_set_unit(dict(original, **{field:bad}))
                result = self.resolve()
                self.assertEqual(result['status'], 'invalid')
                before = self.bytes_snapshot()
                discovered = module.list_retained_claim_candidates(self.source)
                self.assertEqual(self.bytes_snapshot(), before)
                self.assertEqual(discovered['claims'], [])

    def test_candidate_ambiguity_is_removed_before_sixteen_reference_limit(self):
        rows = [dict(id=f'c{i:02}',summary=f'Condition {i}',source='user-explicit',admitted=True,
                     status='unmet',work_unit_id='many-unit',contract_revision=1) for i in range(18)]
        self.core.record_turn(root=self.root/'intent', platform='codex', session_id='session-1',
            intent_delta={'acceptance_criteria':rows}, expected_input_event_id=self.read_state()['latest_input_event_id'])
        from response_route_fixture import register_route
        register_route(self.core,self.root/'intent',selected=['many-unit'])
        receipt = self.prepare(); self.proof()
        declarations = '\n'.join(f"  - {r['id']}: {r['summary']} [source: user-explicit]" for r in rows)
        claims = '\n'.join(
            f"  - claim: {'Shared ambiguous statement' if i in (0,17) else 'Unique statement '+str(i)}\n"
            f"    criterion: {r['id']}\n    evidence: {self.evidence_source} at {self.verified_at}\n    verdict: pass"
            for i,r in enumerate(rows))
        self.completion = ('[completion-check]\n- verification-before-completion: done\n'
            '- skill-call: verification-before-completion (this turn)\n- acceptance-criteria:\n'
            +declarations+'\n- claim-evidence-map:\n'+claims+'\n- unverified: none\n')
        dependency=self.root/'candidate-only.txt';dependency.write_text('original')
        self.publish(receipt, dependencies=[str(dependency)]); self.stop()
        dependency.unlink();dependency.mkdir()
        spec = importlib.util.spec_from_file_location('bounded_candidates_under_test', MODULE)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        before = self.bytes_snapshot()
        result = module.list_retained_claim_candidates(self.source, limit=16)
        self.assertEqual(self.bytes_snapshot(), before)
        self.assertEqual(result['status'], 'candidates')
        self.assertNotIn('Shared ambiguous statement', [c['statement'] for c in result['claims']])
        self.assertEqual(len(result['claims']), 16)
        self.assertTrue(all(c['validity']=='candidate' for c in result['claims']))

    def test_identical_candidate_references_are_deduplicated(self):
        receipt=self.prepare();self.proof()
        claim_start=self.completion.index('  - claim: Saved report matches input')
        claim_end=self.completion.index('  - claim: Protected file is unchanged')
        duplicate=self.completion[claim_start:claim_end]
        self.completion=self.completion[:claim_end]+duplicate+self.completion[claim_end:]
        self.publish(receipt);self.stop()
        spec=importlib.util.spec_from_file_location('duplicate_candidates_under_test',MODULE)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        before=self.bytes_snapshot()
        result=module.list_retained_claim_candidates(self.source)
        self.assertEqual(self.bytes_snapshot(),before)
        self.assertEqual(len(result['claims']),2)
        self.assertEqual(sum(c['statement']=='Saved report matches input' for c in result['claims']),1)

    def test_malformed_claims_fail_closed(self):
        self.accepted()
        for claims in [[dict(self.claim, proof_digest='invalid')], [dict(self.claim, contract_revision=True)],
                       [dict(self.claim, unexpected='path')], [], None, 'bad']:
            with self.subTest(claims=claims):
                if claims is None: continue
                self.assertEqual(self.resolve(claims)['status'], 'invalid')


if __name__ == '__main__':
    unittest.main()
