"""Record captured proof after input rebinding without repeating verification."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import subprocess
import sys
from unittest import mock

from test_completion_contract_v2 import _CompletionV2Fixture
import test_completion_publication as fixture


class RetainedOriginPublicationTest(_CompletionV2Fixture):
    def retained(self):
        original=self.prepare()
        self.proof()
        self.followup()
        return original,self.prepare()

    def test_current_publication_ingests_original_capture_without_reverification(self):
        original,current=self.retained()
        proof,time=self.completion,self.verified_at
        with self.assertRaisesRegex(ValueError,'Current input'):
            self.publish(original)
        result=self.publish(current)
        self.assertEqual(result['status'],'pending-adapter')
        self.assertEqual(result['receipt_token'],current['receipt_token'])
        evidence=result['original_evidence'][0]
        self.assertEqual(evidence['captured_at'],original['prepared_at'])
        self.assertEqual(evidence['verified_at'],time)
        self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'complete')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            stored=store.completion_read('evidence',result['evidence_references'][0])
            self.assertEqual(stored['proof'],proof)
            self.assertEqual(stored['origin_token'],original['origin_token'])
        self.assertEqual(self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=[]),result)

    def test_cli_retained_capture_option(self):
        original,current=self.retained()
        result=subprocess.run([sys.executable,'-B',str(fixture.SCRIPT),'publish',
            '--schema-version','autopilot-completion-receipt.v2','--receipt-token',current['receipt_token'],
            '--origin-receipt-token',original['receipt_token'],'--verified-at',self.verified_at,
            '--original-dependencies-json','[]','--evidence-source',self.evidence_source,'--completion-file','-'],input=self.completion,text=True,capture_output=True,
            env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1'},cwd=self.root)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['original_evidence'][0]['captured_at'],original['prepared_at'])

    def test_original_capture_still_requires_proof_after_capture(self):
        original,current=self.retained()
        self.proof((datetime.fromisoformat(original['prepared_at'])-timedelta(seconds=2)).isoformat())
        with self.assertRaisesRegex(ValueError,'follow ORIGINAL capture'):
            self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=[])
        self.assertTrue(all(c['status']=='unmet' for c in self.read_state()['acceptance_criteria']))

    def test_changed_context_rejects_original_capture(self):
        original=self.prepare();self.proof()
        self.change_intent({'constraints':['new relevant constraint']},new_input=True)
        self.arguments['input_event_id']=self.read_state()['latest_input_event_id']
        current=self.prepare()
        with self.assertRaisesRegex(ValueError,'contract|context|compatible'):
            self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=[])

    def test_changed_revision_rejects_original_capture(self):
        original=self.prepare();self.proof()
        rows=deepcopy(self.read_state()['acceptance_criteria']);rows[0]['summary']='Changed report condition'
        self.change_intent({'acceptance_criteria':rows},new_input=True)
        self.arguments['input_event_id']=self.read_state()['latest_input_event_id']
        current=self.prepare()
        with self.assertRaisesRegex(ValueError,'contract|revision|compatible'):
            self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=[])

    def test_immutable_but_foreign_receipt_bindings_reject(self):
        original,current=self.retained()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            saved=store.completion_read('receipt',original['receipt_token'])
            candidates=[]
            for key,value in [('session_id','foreign'),('platform','claude'),('intent_root',str(self.root/'foreign'))]:
                changed=deepcopy(saved);changed['binding'][key]=value
                candidates.append(store.completion_put('receipt',changed))
            changed=deepcopy(saved);changed['target']['path']=str(self.root/'other-run')
            candidates.append(store.completion_put('receipt',changed))
            changed=deepcopy(saved);changed['selected_criteria']=[]
            candidates.append(store.completion_put('receipt',changed))
        for token in candidates:
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.publish(current,origin_receipt_token=token,original_dependency_snapshots=[])

    def test_reference_reuse_cannot_combine_origin_ingestion(self):
        original,current=self.retained()
        with self.assertRaisesRegex(ValueError,'Reference|reference'):
            self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=[],completion_check=None,
                verified_at=None,evidence_references=[])

    def test_changed_artifact_cannot_replace_original_dependency_snapshot(self):
        import hashlib
        artifact=self.root/'original-result.txt';artifact.write_text('verified bytes')
        snapshots=[dict(locator=str(artifact),digest='sha256:'+hashlib.sha256(artifact.read_bytes()).hexdigest())]
        original,current=self.retained()
        artifact.write_text('changed after verification')
        with self.assertRaisesRegex(ValueError,'original evidence|coverage|dependency'):
            self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=snapshots)
        self.assertTrue(all(c['status']=='unmet' for c in self.read_state()['acceptance_criteria']))

    def test_unchanged_original_dependency_snapshot_is_preserved(self):
        import hashlib
        artifact=self.root/'original-result.txt';artifact.write_text('verified bytes')
        snapshots=[dict(locator=str(artifact),digest='sha256:'+hashlib.sha256(artifact.read_bytes()).hexdigest())]
        original,current=self.retained()
        result=self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=snapshots)
        self.assertEqual(result['original_evidence'][0]['dependencies'],snapshots)

    def test_origin_ingestion_requires_explicit_original_dependencies(self):
        original,current=self.retained()
        with self.assertRaisesRegex(ValueError,'original dependency|Original dependency'):
            self.publish(current,origin_receipt_token=original['receipt_token'])
        with self.assertRaisesRegex(ValueError,'dependency|dependencies'):
            self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=[],dependencies=[str(self.root/'new')])

    def test_missing_original_receipt_is_unavailable(self):
        original,current=self.retained()
        with self.assertRaisesRegex(ValueError,'Original prepared receipt is unavailable'):
            self.publish(current,origin_receipt_token='sha256:'+'0'*64,original_dependency_snapshots=[])

    def test_missing_original_dependency_stays_unavailable(self):
        original,current=self.retained()
        missing=dict(locator=str(self.root/'missing-original-artifact'),digest='sha256:'+'0'*64)
        with self.assertRaises(OSError):
            self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=[missing])
        self.assertTrue(all(c['status']=='unmet' for c in self.read_state()['acceptance_criteria']))

    @mock.patch("completion_assessment._capture_binding", return_value=None)
    def test_ambiguous_original_time_without_causal_capture_stays_unverified(self, _capture):
        original,current=self.retained()
        self.proof(datetime.fromisoformat(original['prepared_at']).replace(microsecond=0).isoformat())
        with self.assertRaisesRegex(ValueError,'precision|ordering|overlap'):
            self.publish(current,origin_receipt_token=original['receipt_token'],original_dependency_snapshots=[])
