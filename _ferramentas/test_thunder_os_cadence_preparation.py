"""Three preparation paths retain their contracts and never mutate providers."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import unittest
from test_thunder_os_campaign_registry import config
from thunder_os.approvals import ApprovalError
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.cadence_preparation import CadencePreparationReadonly
from thunder_os.db import Repository
from thunder_os.instantly_reader import CAMPAIGN

class PreparationTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.repo=Repository(Path(t.name)/'state.db')
        self.registry=CampaignRegistry(self.repo)
        for name,platform in [('BR-APOLLO-01','apollo'),(CAMPAIGN,'instantly'),('MX27-MTY','codex_local')]:
            self.registry.register({**config(),'campaign_id':name,'platform':platform},'ygor',0)
        self.journeys=Mock();self.instantly=Mock();self.accounts=Mock()
        self.accounts.list_accounts.return_value={'accounts':[]}
        self.instantly.snapshot.return_value={'read_only':True,'provider':'instantly','complete':True,
            'steps':[{'subject':'Original','content':'Unchanged static text.'}]}
        self.reader=CadencePreparationReadonly(SimpleNamespace(repository=self.repo),
            journeys=self.journeys,instantly=self.instantly,accounts=self.accounts)
        for key in ('socket.socket.connect','socket.getaddrinfo'):
            g=patch(key,side_effect=AssertionError('network forbidden'));m=g.start()
            self.addCleanup(g.stop);self.addCleanup(m.assert_not_called)

    def test_apollo_only_reads_existing_draft_and_prompt_inputs(self):
        self.repo.put_states_atomic([('journey_draft','j-test',{'campaign_id':'BR-APOLLO-01','name':'Draft'},0)])
        self.journeys.get.return_value={'id':'j-test','name':'Draft','approval_mode':'apollo_prompts_v1',
            'steps':[{'content':'Original {{variable}}'}],'can_export':False}
        before=deepcopy(self.repo.list_state('journey_draft'))
        result=self.reader.snapshot('BR-APOLLO-01')
        self.assertEqual(result['approval_contract']['kind'],'apollo_prompts_v1')
        self.assertIs(result['read_only'],True)
        self.assertIs(result['execution_authorized'],False)
        self.assertEqual(result['drafts'][0]['steps'][0]['content'],'Original {{variable}}')
        self.assertEqual(result['drafts'][0]['review_url'],'#jornada/nova/j-test/4')
        self.assertEqual(before,self.repo.list_state('journey_draft'))
        self.instantly.snapshot.assert_not_called()

    def test_instantly_remains_own_sequence_without_campaign_change(self):
        result=self.reader.snapshot(CAMPAIGN)
        self.assertEqual(result['approval_contract']['kind'],'instantly_exact_copy_or_hold_dynamic')
        self.assertEqual(result['sequence']['steps'][0]['content'],'Unchanged static text.')
        self.assertFalse(result['enrollment_enabled']);self.assertEqual(result['external_writes'],0)
        self.journeys.get.assert_not_called()

    def test_thunder_only_keeps_explicit_campaign_binding(self):
        self.accounts.list_accounts.return_value={'accounts':[{'id':'source-1','campaign_id':'MX27-MTY','source':{'sha256':'a'}},
            {'id':'source-2','campaign_id':None,'source':{'sha256':'b'}}]}
        self.accounts.account.return_value={'id':'source-1','source':{'sha256':'a'},
            'tabs':{'cadence':[{'text':'Exact approved draft, never sent.'}]}}
        result=self.reader.snapshot('MX27-MTY')
        self.assertEqual(result['approval_contract']['kind'],'thunder_exact_touch')
        self.assertEqual(len(result['accounts']),1)
        self.assertEqual(result['accounts'][0]['touches'][0]['text'],'Exact approved draft, never sent.')
        self.accounts.account.assert_called_once_with('source-1',campaign='MX27-MTY')

    def test_unknown_campaign_does_not_call_provider(self):
        with self.assertRaises(ApprovalError):self.reader.snapshot('BR-UNREGISTERED')
        self.instantly.snapshot.assert_not_called();self.journeys.get.assert_not_called()

    def test_incomplete_provider_cannot_turn_into_complete_preparation(self):
        self.instantly.snapshot.return_value={'read_only':True,'complete':False,'steps':[]}
        result=self.reader.snapshot(CAMPAIGN)
        self.assertIn('provider_sequence_incomplete',result['limitations'])
        self.assertFalse(result['enrollment_enabled'])
