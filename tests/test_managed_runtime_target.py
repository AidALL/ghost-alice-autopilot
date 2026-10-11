"""Managed v2 records live in owned runtime storage, not business cwd."""
from pathlib import Path
from unittest import mock

from test_completion_contract_v2 import _CompletionV2Fixture


class ManagedRuntimeTargetTest(_CompletionV2Fixture):
    def managed(self, cwd=None):
        self.source['GHOST_ALICE_GHOST_ROOT']=str(self.root/'runtime')
        if cwd is not None:
            self.source['GHOST_ALICE_AUTOPILOT_CWD']=str(cwd)
            self.source['PWD']=str(cwd)
        return self.root/'runtime/autopilot/sessions/codex/session-1'

    def test_pretool_publish_stop_share_owned_target_without_business_cwd_writes(self):
        import autopilot_pretool
        import autopilot_runtime_context as runtime
        import autopilot_storage
        business=self.root/'read-only-business';business.mkdir();business.chmod(0o500)
        self.addCleanup(business.chmod,0o700)
        expected=self.managed(business)
        atomic=autopilot_storage._atomic
        def guarded(path,value):
            self.assertFalse(Path(path).is_relative_to(business),'AP attempted a business-directory write')
            return atomic(path,value)
        with mock.patch.object(autopilot_storage,'_atomic',side_effect=guarded):
            receipt=autopilot_pretool.capture_before_tool(self.source,
                dict(hook_event_name='PreToolUse',session_id='session-1',cwd=str(business)))
            self.assertEqual(receipt.get('run_dir'),str(expected),receipt)
            self.assertEqual(runtime.resolve_run_target(self.source)['project_cwd'],business)
            self.proof();published=self.publish(receipt)
            self.assertEqual(published['status'],'pending-adapter')
            applied=self.stop()
            self.assertEqual(applied['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertEqual(list(business.iterdir()),[])
        self.assertTrue((expected/'authority.json').is_file())

    def test_existing_bound_project_receipt_keeps_target_and_original_proof(self):
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        expected=self.managed()
        import autopilot_runtime_context as runtime
        self.assertEqual(runtime.resolve_run_target(self.source)['run_dir'],self.run_dir)
        self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertFalse(expected.exists())
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            proof=store.completion_read('evidence',published['evidence_references'][0])
            self.assertEqual(proof['verified_at'],self.verified_at)
            self.assertEqual(proof['target']['path'],str(self.run_dir))

    def test_bound_logical_alias_precedes_equivalent_managed_default(self):
        import autopilot_runtime_context as runtime
        physical=self.managed();physical.mkdir(parents=True)
        alias=self.root/'logical-target';alias.symlink_to(physical,target_is_directory=True)
        self.source['GHOST_ALICE_AUTOPILOT_RUN_DIR']=str(alias)
        original=self.prepare();self.proof();published=self.publish(original)
        self.source.pop('GHOST_ALICE_AUTOPILOT_RUN_DIR')
        selected=runtime.resolve_run_target(self.source)
        self.assertEqual(selected['run_dir'],alias)
        self.assertEqual(selected['reason'],'explicit-run-dir')
        self.assertFalse(selected['derived_run_dir'])
        current=self.prepare()
        self.assertEqual(current['receipt_token'],original['receipt_token'])
        self.assertEqual(current['target_reference'],original['target_reference'])
        applied=self.stop()['completion_recording']['units'][0]
        self.assertEqual(applied['unit_status'],'complete')
        self.assertEqual(applied['original_evidence'],published['original_evidence'])
        with self.helper.storage.transaction(alias,self.source,migrate_legacy=False) as store:
            receipt=store.completion_read('receipt',original['receipt_token'])
            evidence=store.completion_read('evidence',published['evidence_references'][0])
            self.assertEqual(evidence['origin_token'],receipt['origin_token'])
            self.assertEqual(evidence['target']['path'],str(alias))

    def test_explicit_invalid_target_does_not_fall_back_to_runtime(self):
        expected=self.managed()
        blocked=self.root/'not-a-directory';blocked.write_text('keep')
        self.source['GHOST_ALICE_AUTOPILOT_RUN_DIR']=str(blocked/'run')
        with self.assertRaises(OSError): self.prepare()
        self.assertFalse(expected.exists())

    def test_session_and_platform_runtime_targets_are_distinct(self):
        import autopilot_runtime_context as runtime
        expected=self.managed()
        self.assertEqual(runtime.resolve_run_target(self.source)['run_dir'],expected)
        for platform,session in [('codex','session-2'),('claude','session-1')]:
            source=dict(self.source,GHOST_ALICE_PLATFORM=platform,GHOST_ALICE_SESSION_ID=session)
            self.assertEqual(runtime.resolve_run_target(source)['run_dir'],self.root/'runtime/autopilot/sessions'/platform/session)

    def test_managed_target_survives_later_business_cwd_change(self):
        import autopilot_runtime_context as runtime
        expected=self.managed();self.prepare()
        source=dict(self.source,GHOST_ALICE_AUTOPILOT_CWD='/Library',PWD='/Library')
        self.assertEqual(runtime.resolve_run_target(source)['run_dir'],expected)

    def test_project_off_prevents_owned_runtime_acquisition(self):
        expected=self.managed()
        marker=self.root/'.autopilot/OFF';marker.parent.mkdir();marker.write_text('off')
        self.assertEqual(self.prepare(),dict(status='skipped',reason='autopilot-off'))
        self.assertFalse(expected.exists())

    def test_relative_runtime_root_is_rejected_before_acquisition(self):
        self.managed();self.source['GHOST_ALICE_GHOST_ROOT']='relative'
        with self.assertRaisesRegex(ValueError,'absolute'):
            self.prepare()

    def test_legacy_bound_target_survives_business_cwd_change(self):
        import autopilot_runtime_context as runtime
        receipt=self.prepare();self.managed()
        source=dict(self.source,GHOST_ALICE_AUTOPILOT_CWD='/Library',PWD='/Library')
        self.assertEqual(runtime.resolve_run_target(source)['run_dir'],Path(receipt['run_dir']))

    def test_missing_bound_descriptor_cannot_silently_relocate_origin(self):
        self.prepare();(self.run_dir/'authority.json').unlink()
        runtime=self.managed()
        with self.assertRaisesRegex(ValueError,'descriptor|bound'):
            self.prepare()
        self.assertFalse(runtime.exists())

    def test_runtime_owner_collision_is_rejected_without_alternate_target(self):
        import json
        runtime=self.managed();runtime.mkdir(parents=True)
        (runtime/'authority.json').write_text(json.dumps(dict(schema_version='autopilot-authority.v1',
            kind='session',platform='codex',session_id='other',ledger_root=str(self.root/'intent'))))
        with self.assertRaisesRegex(ValueError,'another session|authority'):
            self.prepare()
        self.assertFalse((self.root/'.autopilot').exists())

    def test_legacy_session_off_skips_authority_lookup_after_runtime_upgrade(self):
        import autopilot_runtime_context as runtime
        import autopilot_storage
        self.prepare()
        (self.run_dir/'OFF').write_text('paused')
        expected=self.managed()
        with mock.patch.object(runtime,'with_discovered_session_root',side_effect=AssertionError('authority discovery while paused')), mock.patch.object(autopilot_storage,'bound_completion_targets',side_effect=AssertionError('receipt lookup while paused')):
            self.assertEqual(self.prepare(),dict(status='skipped',reason='autopilot-off'))
        self.assertFalse(expected.exists())
