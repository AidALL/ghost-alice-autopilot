"""Fixture source integration via generated registered hooks; not native-host proof."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import base64
import shlex
from test_completion_contract_v2 import _CompletionV2Fixture
import test_completion_publication as fixtures


class ManagedLocationChainTest(_CompletionV2Fixture):
    def install(self):
        sandbox=shutil.which('sandbox-exec')
        if not sandbox:self.skipTest('independent OS write-deny is unavailable')
        self.sandbox=[sandbox,'-p','(version 1) (allow default) (deny file-write*) (allow file-write* (literal "/dev/null")) (allow file-write* (subpath '+json.dumps(str(self.root))+'))']
        self.destination=dict(mode='isolated',owned_root=str(self.root),
            platform_dir=str(self.root/'platform'),skills_dir=str(self.root/'skills'),
            ghost_root=str(self.root/'ghost'),ledger_root=str(self.root/'intent'),
            project_root=str(self.root),core_source_root=self.source['GHOST_ALICE_CORE_ROOT'])
        shutil.copytree(fixtures.SKILL,self.root/'skills/autopilot-mode')
        (self.root/'tmp').mkdir()
        descriptor=self.root/'destination.json';descriptor.write_text(json.dumps(self.destination))
        self.install_env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(self.root/'tmp'))
        for key in ('GHOST_ALICE_HOOK_SHARED_DIR','GHOST_ALICE_RUNTIME_SHARED_DIR'):
            self.install_env.pop(key,None)
        # No HOME, CODEX_HOME or native config is changed; use the actual public CLI.
        self.installer_cli=[sys.executable,'-B',str(Path(self.source['GHOST_ALICE_CORE_ROOT'])/'_shared/install_hooks.py'),
            '--platform','codex','--destination-file',str(descriptor),'--addon-source',str(fixtures.ROOT)]
        result=subprocess.run([*self.sandbox,*self.installer_cli],cwd=self.root,env=self.install_env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.hooks=json.loads((self.root/'platform/hooks.json').read_text())['hooks']

    def fire(self,event,marker,**payload):
        command=next(h['command'] for row in self.hooks[event] for h in row['hooks'] if marker in h['command'])
        env=dict(os.environ,GHOST_ALICE_SESSION_INTENT_ROOT=str(self.root/'hostile'),
            GHOST_ALICE_CORE_ROOT=str(self.root/'missing-core'),
            GHOST_ALICE_COMPLETION_SCHEMA='autopilot-completion-receipt.v1',
            PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(self.root/'tmp'))
        result=subprocess.run([*self.sandbox,'/bin/sh','-c',command],input=json.dumps(dict(hook_event_name=event,
            session_id='session-1',cwd=str(self.root),**payload)),cwd=self.root,env=env,
            text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        return json.loads(result.stdout)


    def test_generated_pretool_prepare_publish_stop_agree_v2(self):
        self.install()
        pretool=self.fire('PreToolUse','[adapter:autopilot-mode] prepare-origin')
        context=pretool['hookSpecificOutput']['additionalContext']
        self.assertIn('Native Stop handles publication', context)
        self.assertNotIn(' prepare ', context)
        self.assertNotIn('--receipt-token', context)
        self.source.update(GHOST_ALICE_GHOST_ROOT=str(self.root/'ghost'),
            GHOST_ALICE_SKILLS_DIR=str(self.root/'skills'))
        counter=self.root/'business-check-count';counter.write_text('1')
        self.assertEqual(counter.read_text(),'1')
        original=self.proof()+f'- evidence: {self.evidence_source}\n'
        self.review(original)
        result=self.fire('Stop','[adapter:autopilot-mode] continue',last_assistant_message=original)
        self.assertFalse(result.get('decision')=='block',result)
        self.assertEqual({row['status'] for row in self.read_state()['acceptance_criteria']},{'met'})
        self.assertEqual(counter.read_text(),'1')
        self.assertFalse((self.root/'.autopilot/sessions/codex/session-1/approved-run.json').exists())
        self.assertFalse((self.root/'hostile').exists())
        for action in ('--status','--uninstall'):
            result=subprocess.run([*self.sandbox,*self.installer_cli,action],cwd=self.root,env=self.install_env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)


    def test_registered_runner_rejects_changed_event_registry_source_and_arguments(self):
        self.install()
        sys.path.insert(0,str(Path(self.source['GHOST_ALICE_CORE_ROOT'])/'_shared'))
        import install_destination
        command=next(h['command'] for row in self.hooks['PreToolUse'] for h in row['hooks']
                     if '[adapter:autopilot-mode] prepare-origin' in h['command'])
        args=shlex.split(command,comments=True)
        bind_index=args.index('--binding-b64')+1
        payload_index=bind_index-2
        binding=install_destination.decode_binding(args[bind_index])
        script=Path(binding['adapter']['script']);original=script.read_bytes()
        before=self.read_state()
        for case in ('event','registry','source','arguments'):
            with self.subTest(case=case):
                current=list(args);event='PreToolUse'
                if case=='event':event='Stop'
                elif case=='registry':
                    changed=json.loads(json.dumps(binding));changed['adapter']['adapter_id']='unknown-adapter'
                    current[bind_index]=install_destination.encode_binding(changed)
                elif case=='source':script.write_bytes(original+b'\n# changed source fixture\n')
                else:
                    inner=base64.urlsafe_b64decode(current[payload_index]).decode()+' --help'
                    current[payload_index]=base64.urlsafe_b64encode(inner.encode()).decode()
                result=subprocess.run([*self.sandbox,*current],cwd=self.root,env=self.install_env,
                    input=json.dumps(dict(hook_event_name=event,session_id='session-1',cwd=str(self.root))),
                    text=True,capture_output=True)
                self.assertEqual(result.returncode,126,result.stdout+result.stderr)
                self.assertEqual(self.read_state(),before)
                script.write_bytes(original)


if __name__ == '__main__': unittest.main()
