"""Read-only panel questions with canonical source IDs, no live model calls."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from thunder_os.agent_gateway import AgentGateway, EnvelopeError
from thunder_os.panel_verification import PanelVerification, VerificationError, HermesVerifyProcess
from thunder_os.agents import AgentJob
from thunder_os.hermes_text_adapter import HermesTextAdapter
from thunder_os.models import payload_hash


class Reader:
    def snapshot(self):
        return {'ok':True,'collected_at':'2026-10-21T23:00:00Z','coverage':{'complete':True},
                'campaigns':[{'id':'PILOT','tasks':[{'id':'11','contact_ids':['123'],
                    'contact_name':'Synthetic Person','title':'[PILOT][E2] Example | EMAIL',
                    'due_at':'2026-10-22T15:00:00Z','status':'WAITING','outcome':'blocked',
                    'reason':'Email not verified','reason_code':'EMAIL_UNVALIDATED'}]}]}
    def timeline(self,ident):
        assert ident=='123'
        return {'contact_id':'123','coverage':{'complete':False},'events':[]}


class PanelVerificationTests(unittest.TestCase):
    def setUp(self):
        self.reader=Reader()
        self.runner=Mock(return_value={'answer':'E2 is held because the address is unverified.',
            'source_ids':['task:11'],'uncertainties':['Mailbox replies are not verified.']})
        self.service=PanelVerification(self.reader,self.runner)

    def test_verificar_nao_escreve(self):
        result=self.service.verify('123','Why was E2 not sent?')
        self.assertEqual(result['sources'][0]['id'],'task:11')
        self.assertIn('/51412240/',result['sources'][0]['url'])
        self.assertFalse(result['external_effects'])
        self.assertTrue(result['read_only'])
        self.runner.assert_called_once()
        self.assertLess(len(json.dumps(self.runner.call_args.args[0]).encode()),24000)

    def test_unknown_contact_and_forged_source_are_rejected(self):
        with self.assertRaisesRegex(VerificationError,'contact_not_confirmed'):
            self.service.verify('124','Why?')
        self.runner.assert_not_called()
        self.runner.return_value={'answer':'Synthetic','source_ids':['task:999'],'uncertainties':[]}
        with self.assertRaisesRegex(VerificationError,'invalid_verification_answer'):
            self.service.verify('123','Why?')

    def test_source_qualifiers_and_server_limits_survive_empty_model_limits(self):
        self.reader.timeline=lambda _: {'coverage':{'complete':False,'limitations':['crm_activity_logs_are_not_external_delivery_receipts']},
            'events':[{'id':'emails:77','status':'SENT','delivery_verified':False,
                'historical_membership_verified':False,'crm_recorded_sent':True,
                'attribution':'contact_current_campaign','timestamp_semantics':'activity_time'}]}
        self.runner.return_value={'answer':'Existe apenas registro CRM.', 'source_ids':['emails:77'],'uncertainties':[]}
        result=self.service.verify('123','Why?')
        packet=self.runner.call_args.args[0]
        source=next(row for row in packet['sources'] if row['id']=='emails:77')
        self.assertFalse(source['delivery_verified'])
        self.assertFalse(source['historical_membership_verified'])
        self.assertEqual(source['attribution'],'contact_current_campaign')
        self.assertIn('crm_activity_logs_are_not_external_delivery_receipts',packet['source_limitations'])
        self.assertGreaterEqual(len(result['server_limits']),4)
        self.assertEqual(result['uncertainties'],[])

    def test_failed_runtime_is_not_retried_or_exposed(self):
        self.runner.side_effect=RuntimeError('PRIVATE credential or provider response')
        with self.assertRaisesRegex(VerificationError,'hermes_unavailable') as error:
            self.service.verify('123','Why?')
        self.assertNotIn('PRIVATE',str(error.exception))
        self.runner.assert_called_once()

    def test_trial_cannot_bootstrap_verification(self):
        from thunder_os.hermes_text_child import configured_adapter
        with self.assertRaisesRegex(ValueError,'read-only operation'):
            configured_adapter(brain=Path('/missing'),evidence=Path('/missing'),
                hermes_root=Path('/missing'),profile=Path('/missing'),mode='trial',capability='verify')

    def test_process_uses_fixed_readonly_environment_and_cleans_owned_scratch(self):
        packet=self.service.context('123','Why?')
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);(root/'config.yaml').write_text('synthetic: true')
            process=HermesVerifyProcess(python=sys.executable,hermes_root=root,profile=root,
                                       runtime_root=root,source_root=root)
            calls=[]
            def transport(command,*,cwd,env):
                self.assertEqual(command[command.index('--mode')+1],'operation')
                self.assertEqual(env['HERMES_AUTH_READ_ONLY'],'1')
                self.assertEqual(env['THUNDER_OS_EXTERNAL_EFFECTS_ENABLED'],'false')
                self.assertEqual(env['HERMES_HOME'],str(root))
                evidence=Path(command[command.index('--evidence')+1])
                self.assertEqual(json.loads(evidence.read_text())['verification_context'],packet)
                def run(job,timeout):
                    calls.append(job);self.assertEqual(timeout,60)
                    return json.dumps(self.runner.return_value)
                return run
            with patch('thunder_os.panel_verification.bounded_process_transport',side_effect=transport):
                self.assertEqual(process(packet,'Why?'),self.runner.return_value)
            self.assertEqual(len(calls),1)
            self.assertEqual(sorted(p.name for p in root.iterdir()),['config.yaml'])

    def test_mudar_rota_exige_clique_and_is_disabled_in_this_round(self):
        self.assertEqual(self.service.capabilities(),{'verify':True,'change_route':False,'send':False})
        with self.assertRaisesRegex(VerificationError,'action_disabled'):
            self.service.change_route({'approved':True})
        self.runner.assert_not_called()

    def test_gateway_verify_does_not_enqueue_work_or_accept_authority_flags(self):
        service=Mock()
        gateway=AgentGateway(service,'.')
        verifier=Mock(return_value={'read_only':True})
        envelope={'actor':'ygor','capability':'verify','contact_id':'123','question':'Why E2?'}
        self.assertTrue(gateway.verify(envelope,verifier)['read_only'])
        self.assertEqual(service.mock_calls,[])
        for changed in ({**envelope,'approved':True},{**envelope,'actor':'hermes'},
                        {**envelope,'capability':'send'}):
            with self.assertRaises(EnvelopeError):gateway.verify(changed,verifier)

    def test_adapter_verify_uses_tools_empty_and_one_existing_runtime_call(self):
        scope={'contact_id':'123','source_ids':['task:11'],'sources':[{'id':'task:11','reason':'held'}]}
        response={'answer':'Held.','source_ids':['task:11'],'uncertainties':[]}
        create=Mock(return_value=iter([
            {'type':'response.output_text.delta','delta':json.dumps(response)},
            {'type':'response.completed','response':{'status':'completed','output':[],
                                                    'usage':{'input_tokens':120,'output_tokens':20}}}]))
        client=SimpleNamespace(responses=SimpleNamespace(create=create),close=Mock())
        factory=Mock(return_value=client)
        adapter=HermesTextAdapter(config_loader=lambda:{'model':{'provider':'openai-codex','default':'configured-model'}},
            runtime_loader=lambda **_:{'provider':'openai-codex','api_mode':'codex_responses',
                'api_key':'synthetic','base_url':'https://fixture.invalid'},
            client_factory=factory,context_loader=lambda _: {'verification_context':scope,'not_a_send_authorization':True})
        job=AgentJob('verify-test','verify',{'contact_id':'123','scope_hash':payload_hash(scope),'question':'Why?'})
        self.assertEqual(json.loads(adapter(job,60)),response)
        self.assertEqual(adapter.usage,{'input_tokens':120,'output_tokens':20})
        self.assertEqual(create.call_count,1)
        self.assertEqual(create.call_args.kwargs['tools'],[])
        self.assertFalse(create.call_args.kwargs['store'])
        self.assertEqual(factory.call_args.kwargs['max_retries'],0)
        client.close.assert_called_once()

if __name__=='__main__':unittest.main()
