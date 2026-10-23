"""Campaign facts, approval freshness and preview conservation; no providers."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit

from thunder_os.approvals import ApprovalError
from thunder_os.campaign_sheet import FIELDS, require_sheet, sheet_view, validate_sheet
from thunder_os.campaign_batches import preview_batches, validate_batches
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.models import payload_hash
from thunder_os.service import ThunderService
from test_campaign_sheet_support import make_sheet
import test_thunder_os_campaign_registry as registry_tests
import test_thunder_os_campaign_confirmation as confirmation_tests
import test_thunder_os_operation_campaign as operation_tests
import painel_servidor as panel


class CampaignSheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store, self.sheet = make_sheet(self.root/'brain')
        self.repo = Repository(self.root/'workflow.db')
        self.registry = CampaignRegistry(self.repo)

    def test_legacy_configuration_hash_is_unchanged(self):
        cfg = registry_tests.config()
        row = self.registry.register(cfg, 'ygor', 0)
        self.assertEqual(row['context']['configuration_hash'], payload_hash({
            'configuration':cfg['configuration'], 'providers':cfg['providers'], 'revision':1}))
        self.assertIsNone(self.registry.sheet('synthetic'))
        self.assertEqual(sheet_view('synthetic',None,self.store)['missing_fields'],list(FIELDS))

    def test_facts_reference_only_and_hash_changes_existing_context(self):
        cfg = {**registry_tests.config(), 'briefing':self.sheet,
               'batches':[{'batch_id':'L01','source_codes':['old-L01']}]}
        first = self.registry.register(cfg,'ygor',0)
        serialized = json.dumps(first['config'])
        self.assertNotIn('Synthetic Oferta',serialized)
        view = require_sheet('synthetic',self.registry.sheet('synthetic'),self.store)
        self.assertEqual(view['status'],'complete')
        self.assertEqual(len(view['fields']),8)
        for row in view['fields']:
            self.assertEqual(row['expected_sha256'],row['observed_sha256'])
            self.assertTrue(row['source_updated_at'])
            self.assertTrue(row['content'])
        cfg['briefing'] = {**self.sheet,'version':'2'}
        second = self.registry.register(cfg,'ygor',1)
        self.assertNotEqual(first['context']['configuration_hash'],second['context']['configuration_hash'])
        self.assertEqual(first['context'].keys(),second['context'].keys())
        self.assertEqual(self.repo.effects(),[])

    def test_missing_fields_and_sources_never_become_complete(self):
        partial = deepcopy(self.sheet)
        del partial['fields']['cta']
        view = sheet_view('synthetic',partial,self.store)
        self.assertEqual(view['missing_fields'],['cta'])
        with self.assertRaisesRegex(ApprovalError,'incomplete'):
            require_sheet('synthetic',partial,self.store)
        (self.store.root/'campaign.md').unlink()
        self.assertTrue(all(r['status']=='missing_file' for r in sheet_view('x',self.sheet,self.store)['fields']))
        self.assertTrue(all(r['status']=='unavailable' for r in sheet_view('x',self.sheet)['fields']))

    def test_multiple_references_require_every_source_and_bind_all_hashes(self):
        sheet = deepcopy(self.sheet)
        other = self.store.root/'other.md'
        other.write_text('## Policy\nOnly the approved proof.\n')
        ref = {'path':'other.md','sha256':hashlib.sha256(other.read_bytes()).hexdigest(),
               'section':'Policy'}
        sheet['fields']['proofs'] = [sheet['fields']['proofs'], ref]
        view = require_sheet('synthetic',sheet,self.store)
        row = next(row for row in view['fields'] if row['key']=='proofs')
        self.assertEqual(len(row['sources']),2)
        self.assertIn('Only the approved proof.',row['content'])
        self.assertEqual(validate_sheet(sheet),sheet)
        other.write_text('## Policy\nChanged.\n')
        with self.assertRaisesRegex(ApprovalError,'changed'):
            require_sheet('synthetic',sheet,self.store)
        row = next(row for row in sheet_view('synthetic',sheet,self.store)['fields'] if row['key']=='proofs')
        self.assertIsNone(row['content'])
        for invalid in ([], [ref]*7, [[ref]]):
            sheet['fields']['proofs']=invalid
            with self.assertRaises(ApprovalError): validate_sheet(sheet)

    def test_invalid_paths_and_unknown_authorization_fields_are_rejected(self):
        for path in ['/etc/a.md','../a.md','x/../a.md','x//a.md','.hidden/a.md','x\\a.md','a.env','x\na.md']:
            invalid = deepcopy(self.sheet)
            invalid['fields']['offer']['path'] = path
            with self.subTest(path=path), self.assertRaises(ApprovalError): validate_sheet(invalid)
        for field in ['authorized','approved_by','allow_send']:
            invalid = {**self.sheet,field:True}
            with self.subTest(field=field), self.assertRaises(ApprovalError): validate_sheet(invalid)
        for ref in [{'path':'campaign.md','sha256':'bad','section':'offer'},
                    {'path':'campaign.md','sha256':'a'*64,'section':''}]:
            invalid = deepcopy(self.sheet); invalid['fields']['offer']=ref
            with self.assertRaises(ApprovalError): validate_sheet(invalid)

    def test_symlinks_fifo_large_and_invalid_utf8_are_unavailable(self):
        path = self.store.root/'campaign.md'
        outside = self.root/'outside.md';outside.write_text('private')
        path.unlink();path.symlink_to(outside)
        self.assertTrue(all(r['status']=='unavailable' for r in sheet_view('x',self.sheet,self.store)['fields']))
        path.unlink();os.mkfifo(path)
        self.assertTrue(all(r['status']=='unavailable' for r in sheet_view('x',self.sheet,self.store)['fields']))
        path.unlink();path.write_bytes(b'x'*65537)
        self.assertTrue(all(r['status']=='unavailable' for r in sheet_view('x',self.sheet,self.store)['fields']))
        path.write_bytes(b'\xff')
        sheet = deepcopy(self.sheet)
        for ref in sheet['fields'].values():ref['sha256']=hashlib.sha256(b'\xff').hexdigest()
        self.assertTrue(all(r['status']=='unavailable' for r in sheet_view('x',sheet,self.store)['fields']))

    def test_section_missing_duplicate_and_empty_are_incomplete(self):
        for raw, state in [(b'## other\ntext','section_missing'),
                           (b'## offer\ntext\n## offer\nmore','section_missing'),
                           (b'## offer\n\n## other\ntext','empty')]:
            (self.store.root/'campaign.md').write_bytes(raw)
            sheet = deepcopy(self.sheet)
            sheet['fields']['offer']['sha256']=hashlib.sha256(raw).hexdigest()
            self.assertEqual(sheet_view('x',sheet,self.store)['fields'][0]['status'],state)

    def test_file_changed_marks_version_old_without_serving_unverified_content(self):
        (self.store.root/'campaign.md').write_text('Changed facts')
        view = sheet_view('synthetic',self.sheet,self.store)
        self.assertTrue(all(r['status']=='hash_mismatch' and r['content'] is None for r in view['fields']))
        with self.assertRaisesRegex(ApprovalError,'changed'):require_sheet('synthetic',self.sheet,self.store)

    def test_batches_are_campaign_children_without_authorization(self):
        valid = [{'batch_id':'L01','source_codes':['MX27-MTY-L01']}]
        self.assertEqual(validate_batches(valid),valid)
        for invalid in [valid+valid,[{'batch_id':'L02','source_codes':['same','same']}],
                        [{'batch_id':'L01','source_codes':['x'],'approved':True}]]:
            with self.assertRaises(ApprovalError):validate_batches(invalid)

    def test_batch_source_code_cannot_have_two_active_campaign_parents(self):
        cfg = {**registry_tests.config(), 'batches':[{'batch_id':'L01','source_codes':['old-L01']}]}
        self.registry.register(cfg,'ygor',0)
        with self.assertRaisesRegex(ApprovalError,'parent_conflict'):
            self.registry.register({**cfg,'campaign_id':'other'},'ygor',0)
        self.assertIsNone(self.repo.get_state('campaign_config','other'))

    def test_absent_sheet_does_not_block_actual_research_submission(self):
        import test_thunder_os_eval_trajectory as trajectory
        import test_thunder_os_research as research
        service = ThunderService(self.repo, approved_catalog=trajectory.CATALOG)
        service.configure_campaign(registry_tests.config(),'ygor',expected_version=0)
        submitted = service.submit_research({'dossier':research.base_artifact()},'ygor')
        self.assertTrue(submitted['facts'])
        self.assertIsNone(self.registry.sheet('synthetic'))

    def test_preview_preserves_every_task_receipt_and_four_statuses_without_writes(self):
        snapshot = {'campaigns':[{'id':'MX27-MTY-L01','tasks':[{'id':str(i),'contact_ids':['123'],
            'receipt':{'state':status,'provider_id':'synthetic:'+str(i)}}
            for i,status in enumerate(['sent']*83+['uncertain']*4+['blocked']*12+['already_registered'])],
            'metrics':{'83':83,'4':4,'12':12,'1':1}},
            {'id':'unreadable-code','tasks':[],'metrics':{}}]}
        before = deepcopy(snapshot)
        preview = preview_batches(snapshot,{'MX27-MTY-L01':{'campaign_id':'MX27-MTY','batch_id':'L01'}})
        self.assertEqual(preview['writes'],0)
        self.assertEqual(snapshot,before)
        self.assertEqual(preview['groups'][0]['tasks'],snapshot['campaigns'][0]['tasks'])
        self.assertEqual(preview['groups'][0]['metrics'],snapshot['campaigns'][0]['metrics'])
        self.assertEqual(preview['groups'][1]['status'],'unchanged')
        self.assertEqual(self.repo.effects(),[])
        self.assertEqual(self.repo.list_state('campaign_config'),[])

    def test_preview_keeps_existing_cdmx_parent_and_recovers_explicit_batch_per_task(self):
        task = {'id':'123','title':'MX27-CDMX-D0-20261021 | E1 | Synthetic | Synthetic',
                'campaign':'MX27-CDMX','receipt':{'id':'synthetic-receipt'}}
        snapshot = {'campaigns':[{'id':'MX27-CDMX','tasks':[task]}]}
        preview = preview_batches(snapshot,{'MX27-CDMX-D0-20261021':{
            'campaign_id':'MX27-CDMX','batch_id':'MX27-CDMX-D0-20261021'}})
        self.assertEqual(preview['groups'][0]['tasks'],[task])
        self.assertEqual(preview['groups'][0]['task_bindings'],[{'task_id':'123',
            'source_code':'MX27-CDMX-D0-20261021','campaign_id':'MX27-CDMX',
            'batch_id':'MX27-CDMX-D0-20261021'}])

    def test_sheet_endpoint_only_ygor_get_and_complete_sources(self):
        service = ThunderService(self.repo)
        service.campaign_sheet_store = self.store
        service.configure_campaign({**registry_tests.config(),'briefing':self.sheet},'ygor',expected_version=0)
        for actor,method,expected in [('ygor','GET',200),('', 'GET',401),('codex','GET',403),('ygor','POST',405)]:
            handler=object.__new__(panel.Handler)
            handler.server=SimpleNamespace(thunder_service=service)
            handler.command=method;handler.client_address=('127.0.0.1',1234)
            handler.headers={'X-Authenticated-User':actor}
            handler.responder=lambda status,body:(status,body)
            if expected==200:
                status,body=handler.thunder_api(urlsplit('/api/os/v1/campaign-sheets'))
                self.assertEqual(status,200)
                self.assertEqual(body['campaigns'][0]['status'],'complete')
                self.assertTrue(body['read_only'])
            else:
                with self.assertRaises(panel.Erro) as exc:
                    handler.thunder_api(urlsplit('/api/os/v1/campaign-sheets'))
                self.assertEqual(exc.exception.status,expected)


@contextmanager
def cadence_fixture():
    fixture=confirmation_tests.CampaignConfirmationTests('runTest')
    fixture.setUp()
    try:yield fixture
    finally:fixture.doCleanups()


class CampaignSheetApprovalTests(unittest.TestCase):
    def test_incomplete_sheet_blocks_approval_but_not_research_lookup(self):
        with cadence_fixture() as f:
            cfg=deepcopy(f.config);cfg['briefing']['fields'].pop('cta')
            f.change_config(**cfg)
            cadence=f.repo.get_state('cadence','batch-1')
            f.repo.put_state('cadence','batch-1',{**cadence,'execution_context':f.canonical})
            self.assertEqual(f.service.current_research('synthetic.invalid'),f.research)
            with self.assertRaisesRegex(ApprovalError,'incomplete'):f.approve()
            self.assertIsNone(f.repo.get_state('approval','batch-1'))
            f.adapter.send.assert_not_called()

    def test_old_registry_version_blocks_queued_text(self):
        with cadence_fixture() as f:
            f.approve();f.queue()
            sheet=deepcopy(f.config['briefing']);sheet['version']='2'
            f.change_config(briefing=sheet)
            f.worker().process_one()
            f.adapter.send.assert_not_called()
            view=f.service.campaign_review_view(f.repo.get_state('cadence','batch-1'))
            self.assertEqual(view['campaign_sheet_label'],'versão anterior')
            self.assertIsNone(view['execution_confirmation'])

    def test_brain_changed_blocks_already_queued_effect_and_new_approval(self):
        with cadence_fixture() as f:
            f.approve();f.queue()
            (f.sheet_store.root/'campaign.md').write_text('Changed after approval')
            f.worker().process_one()
            f.adapter.send.assert_not_called()
            with self.assertRaisesRegex(ApprovalError,'changed'):f.approve()
            view=f.service.campaign_review_view(f.repo.get_state('cadence','batch-1'))
            self.assertEqual(view['campaign_sheet_status'],'stale')

    def test_secondary_source_change_blocks_queued_effect(self):
        with cadence_fixture() as f:
            path=f.sheet_store.root/'secondary.md'
            path.write_text('## Restrictions\nNo external effects.\n')
            sheet=deepcopy(f.config['briefing'])
            sheet['fields']['restrictions']=[sheet['fields']['restrictions'],
                {'path':'secondary.md','section':'Restrictions',
                 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]
            f.change_config(briefing=sheet)
            cadence=f.repo.get_state('cadence','batch-1')
            f.repo.put_state('cadence','batch-1',{**cadence,'execution_context':f.canonical})
            f.approve();f.queue()
            path.write_text('## Restrictions\nChanged after approval.\n')
            f.worker().process_one()
            f.adapter.send.assert_not_called()
            self.assertEqual(f.service.campaign_review_view(
                f.repo.get_state('cadence','batch-1'))['campaign_sheet_status'],'stale')

    def test_manual_operation_also_revalidates_brain_before_effect(self):
        f=operation_tests.OperationCampaignTests('runTest');f.setUp()
        try:
            record=f.approve();f.attempt()
            (f.sheet_store.root/'campaign.md').write_text('Changed after approval')
            with self.assertRaisesRegex(ApprovalError,'changed'):
                with f.gate.execution_guard(record,'seqd_attempt','effect1'):
                    self.fail('provider boundary entered')
        finally:f.doCleanups()


if __name__=='__main__':unittest.main()
