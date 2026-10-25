"""Isolated full-chain fixtures. No network, production DB, campaigns or real mail."""
from copy import deepcopy
from datetime import datetime,timezone
from email.utils import format_datetime
from pathlib import Path
import time
import unittest
from unittest.mock import Mock,patch
from thunder_os.models import payload_hash
from thunder_os.worker import WorkerRuntime
from thunder_os.email_readback import GmailReceiptReader
from thunder_os.thunder_email_runtime import GmailCadenceAdapter,ThunderEmailCycle,configured_adapter,configured_cycle,_hash
from thunder_os.seqd_client import MAILBOX_ID,WORKSPACE_ID
from thunder_os.seqd_client import MAILBOX
from thunder_os.contact_history import ContactHistoryReadonly
from test_thunder_os_campaign_confirmation import CampaignConfirmationTests
from test_thunder_os_external_email_import import FakeCRM,record

class Mail:
 def __init__(self):self.sends=[];self.fail=False;self.envelope=None;self.reads=[];self.date=format_datetime(datetime.now(timezone.utc))
 def status(self):return {'mailbox':MAILBOX,'status':'ready','send_enabled':True}
 def send(self,envelope):
  self.envelope=deepcopy(envelope);self.sends.append(deepcopy(envelope))
  if self.fail:raise TimeoutError('synthetic lost response')
  return {'mailbox':MAILBOX,'status':'sent','provider_id':'abc123'}
 def reconcile(self,key):
  self.reads.append(key);return {'mailbox':MAILBOX,'status':'sent','provider_id':'abc123','message_id':self.message_id()}
 def message_id(self):
  e=self.envelope
  return '<thunderos.'+_hash({'mailbox':MAILBOX_ID,'workspace':WORKSPACE_ID,**{k:e[k] for k in ('to','subject','body','thread_id')}})+'@thundermustard.com>'
 def read(self,ident):
  e=self.envelope
  return {'mailbox':MAILBOX,'status':'ok','message':{'id':ident,'thread_id':'def456','from':MAILBOX,
   'to':e['to'],'subject':e['subject'],'body':e['body'],'date':self.date,
   'sent':True,'cc':'','bcc':'','truncated':False,'message_id':self.message_id()}}
 def conversation(self,ident):
  e=self.envelope
  def msg(mid,outgoing):return {'id':mid,'thread_id':ident,'from':MAILBOX if outgoing else e['to'],
   'to':e['to'] if outgoing else MAILBOX,'cc':'','bcc':'','labelIds':['SENT'] if outgoing else ['INBOX'],
   'internal_date':'1792933200000' if outgoing else '1792933260000','subject':e['subject'],
   'bodies':{'text':e['body'] if outgoing else 'Synthetic reply: stop.'},'complete':True,'coverage':{'reasons':[]}}
  return {'mailbox':MAILBOX,'status':'ok','thread':{'id':ident,'messages':[msg('abc123',True),msg('abc124',False)]}}

class ThunderEmailTests(unittest.TestCase):
 def setUp(self):
  CampaignConfirmationTests.setUp(self)
  guard=patch('http.client.HTTPConnection',side_effect=AssertionError('network forbidden'));guard.start();self.addCleanup(guard.stop)
  guard=patch('http.client.HTTPSConnection',side_effect=AssertionError('network forbidden'));guard.start();self.addCleanup(guard.stop)
  self.config['providers']={'email':'gmail'};self.config['configuration']['provider_accounts']={'email':MAILBOX}
  registry=self.service.configure_campaign(self.config,'ygor',expected_version=self.registry_row['version'])
  second={**self.step,'id':'s2','order':2,'subject':'Synthetic second, never sent','content':'Never send this fixture', 'predecessor_id':'s1'}
  self.cadence=self.repo.put_state('cadence','batch-1',{**self.cadence,'execution_context':registry['context'],
      'hubspot_contact_id':'100','steps':[self.step,second],'status':'ready_for_review'})
  CampaignConfirmationTests.approve(self)
  self.job=CampaignConfirmationTests.queue(self)
  self.mail=Mail();self.policy={'version':1,'approval_ref':'synthetic-approval','mailbox':MAILBOX,
   'campaign_id':self.config['campaign_id'],'cadence_id':'batch-1','step_id':'s1','effect_key':self.job['effect_key'],
   'recipient':self.step['recipient'],'subject':self.step['subject'],'content_hash':payload_hash(self.step['content']),
   'issued_at':time.time()-1,'expires_at':time.time()+250,'max_attempts':1}
  self.adapter=GmailCadenceAdapter(self.repo,client=self.mail,enabled=True,policy_loader=lambda:deepcopy(self.policy))
  self.worker=WorkerRuntime(self.repo,self.ledger,{'email':self.adapter},worker_id='synthetic-worker',
    health_path=Path(self.tmp.name)/'health.json',revision='synthetic')
  self.worker.authorization_service.current_research=lambda _:self.research
  self.worker.authorization_service.campaign_sheet_store=self.sheet_store
  self.reader=GmailReceiptReader(self.repo,self.mail)
  self.crm=FakeCRM(self.repo);self.crm.contact=record('100',self.step['recipient'],'')
  self.cycle=ThunderEmailCycle(self.repo,reader=self.reader,client=self.mail,crm=self.crm,
    crm_enabled=True,authorize=lambda plan:plan['contact_id']=='100',reply_enabled=True)
 def sent(self):
  self.assertTrue(self.worker.process_one());self.assertEqual(self.repo.effect(self.job['id'])['state'],'sent')
 def test_whole_existing_worker_receipt_email_reply_stop(self):
  self.sent();self.assertEqual(len(self.mail.sends),1);self.assertEqual(len(self.ledger.calls),1)
  self.assertEqual(self.mail.sends[0]['to'],self.step['recipient'])
  plan=self.cycle.prepare_email('gmail:abc123');result=self.cycle.log_email(plan)
  self.assertTrue(result['email_logged']);self.assertEqual(len(self.crm.writes),1)
  self.assertEqual(self.crm.writes[0]['properties']['hs_email_direction'],'EMAIL')
  self.assertEqual(self.cycle.log_email(plan)['email_logged'],True);self.assertEqual(len(self.crm.writes),1)
  second=self.service.dispatch_step('batch-1','s2','ygor')
  event=ContactHistoryReadonly._gmail_event(self.mail.conversation('def456')['thread']['messages'][1],'def456',{self.step['recipient']})
  result=self.cycle.receive_reply('gmail:abc123','def456','abc124',review={'actor':'ygor','human_reply':True,'message_hash':payload_hash(event)})
  self.assertEqual(result['cadence_readback']['status'],'paused_reply')
  self.assertEqual(result['cadence_readback']['steps'][1]['status'],'skipped_reply')
  self.worker.process_one();self.assertEqual(self.repo.effect(second['id'])['state'],'failed')
  self.assertEqual(self.repo.effect(second['id'])['error_code'],'cadence_not_approved')
  self.assertEqual(len(self.mail.sends),1)
  self.assertTrue(self.repo.list_state('reply'));self.assertTrue(self.repo.list_state('operator_alert'))
 def test_direct_adapter_send_requires_worker_fence(self):
  self.adapter.prepare_job(self.job)
  effect={'target':self.step['recipient'],'subject':self.step['subject'],'content':self.step['content'],'effect_id':'s1'}
  with self.assertRaisesRegex(ValueError,'dispatch_fence'):self.adapter.send(effect)
  self.repo._local.dispatch_fence=('other','other')
  try:
   with self.assertRaisesRegex(ValueError,'dispatch_fence'):self.adapter.send(effect)
  finally:self.repo._local.dispatch_fence=None
  self.assertEqual(self.mail.sends,[])
 def test_contact_change_during_receipt_read_blocks_authorization(self):
  self.sent();plan=self.cycle.prepare_email('gmail:abc123');original=self.reader.read_sent_event
  def changed(*args):
   event=original(*args);cadence=self.repo.get_state('cadence','batch-1')
   self.repo.put_state('cadence','batch-1',{**cadence,'hubspot_contact_id':'200'})
   return event
  with patch.object(self.reader,'read_sent_event',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'binding_changed'):self.cycle._authorize(plan)
  self.assertEqual(self.crm.writes,[])
 def test_reply_preview_reads_provider_without_pausing(self):
  self.sent();preview=self.cycle.preview_reply('gmail:abc123','def456','abc124')
  self.assertEqual(preview['message_hash'],payload_hash(preview['message']))
  self.assertEqual(self.repo.get_state('cadence','batch-1')['status'],'approved')
 def test_attempt_survives_new_adapter_and_policy_renewal(self):
  self.adapter.prepare_job(self.job);self.policy['approval_ref']='synthetic-renewal'
  fresh=GmailCadenceAdapter(self.repo,client=self.mail,enabled=True,policy_loader=lambda:self.policy)
  with self.assertRaisesRegex(ValueError,'already_reserved'):fresh.prepare_job(self.job)
  self.assertEqual(self.mail.sends,[])
 def test_panel_routes_require_actor_and_disable_crm_by_default(self):
  import painel_servidor as panel
  from types import SimpleNamespace
  from urllib.parse import urlsplit
  handler=object.__new__(panel.Handler);handler.command='POST'
  handler.server=SimpleNamespace(thunder_service=self.service,thunder_email_cycle=self.cycle)
  handler.responder=lambda status,body:(status,body)
  handler.thunder_actor=Mock(return_value='claude');handler.corpo=lambda:{'receipt':'gmail:abc123'}
  path='/api/os/v1/integrations/thunder-email/'
  with self.assertRaises(panel.Erro):handler.thunder_api(urlsplit(path+'receipt-preview'))
  handler.thunder_actor.assert_called_once()
  self.sent();handler.thunder_actor=Mock(return_value='ygor')
  result=handler.thunder_api(urlsplit(path+'receipt-preview'))
  self.assertEqual(result[0],200);self.assertEqual(result[1]['contact_id'],'100')
  handler.corpo=lambda:{'plan':result[1]}
  with patch.dict('os.environ',{},clear=True):
   with self.assertRaises(panel.Erro):handler.thunder_api(urlsplit(path+'email-execute'))
  self.assertEqual(self.crm.writes,[])
  handler.corpo=lambda:{'receipt':'gmail:abc123','thread_id':'def456','message_id':'abc124'}
  preview=handler.thunder_api(urlsplit(path+'reply-preview'))[1]
  handler.corpo=lambda:{'receipt':'gmail:abc123','thread_id':'def456','message_id':'abc124',
   'review':{'actor':'ygor','human_reply':True,'message_hash':preview['message_hash']}}
  result=handler.thunder_api(urlsplit(path+'reply-ingest'))
  self.assertEqual(result[1]['cadence_readback']['status'],'paused_reply')
  self.assertEqual(len(self.mail.sends),1)
 def test_authority_revoked_during_gmail_read_blocks(self):
  self.sent();plan=self.cycle.prepare_email('gmail:abc123');authority={'allowed':True}
  self.cycle.authorize=lambda _:authority['allowed'];original=self.reader.read_sent_event
  def changed(*args):
   event=original(*args);authority['allowed']=False;return event
  with patch.object(self.reader,'read_sent_event',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'authority_required'):self.cycle._authorize(plan)
  self.assertEqual(self.crm.writes,[])
 def test_generated_worker_envelope_matches_real_bridge_contract(self):
  import json,subprocess
  self.sent()
  script="const c=require('./_ferramentas/seqd_bridge/contract.cjs');let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>{let e=JSON.parse(s);const key='a'.repeat(64);e.approval_signature=c.signature(e,key);c.validate(e,key,Date.now());process.stdout.write('PASS');});"
  result=subprocess.run(['node','-e',script],input=json.dumps(self.mail.envelope),text=True,capture_output=True,check=True)
  self.assertEqual(result.stdout,'PASS')
 def test_runtime_factory_composes_same_repository_disabled_by_default(self):
  from thunder_os.worker import build_runtime
  with patch.dict('os.environ',{'THUNDER_EXTERNAL_EFFECTS_ENABLED':'true','THUNDER_OS_DB':str(Path(self.tmp.name)/'factory.sqlite'),
       'THUNDER_OS_WORKER_HEALTH':str(Path(self.tmp.name)/'factory-health.json')},clear=True),patch('thunder_os.worker.TouchLedger'):
   runtime=build_runtime()
  self.assertIs(runtime.repository,runtime.adapters['email'].repo)
  self.assertEqual(runtime.adapters['email'].capability,'disabled')
 def reconciliation_fixture(self):
  from test_thunder_os_eval_trajectory import EvalTrajectoryTests
  scenario=EvalTrajectoryTests();scenario.setUp();self.addCleanup(scenario.doCleanups)
  self.worker.ledger=scenario.ledger;self.ledger=scenario.ledger
  self.mail.fail=True;self.worker.process_one()
  self.cycle.ledger=self.ledger;self.cycle.reconcile_enabled=True
  return scenario
 def test_uncertain_real_ledger_confirmation_never_resends(self):
  scenario=self.reconciliation_fixture()
  preview=self.cycle.preview_reconciliation('batch-1','s1')
  result=self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])
  self.assertEqual(result['status'],'confirmed');self.assertEqual(len(self.mail.sends),1)
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'sent')
  self.assertEqual(self.cycle.prepare_email('gmail:abc123')['contact_id'],'100')
  preview=self.cycle.preview_reconciliation('batch-1','s1')
  self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])
  self.worker.process_one();self.assertEqual(len(self.mail.sends),1)
  with scenario.database() as (con,_):
   self.assertEqual(con.execute('SELECT status,provider_id FROM toques').fetchall(),[('feito','gmail:abc123')])
 def test_reconciliation_wrong_nonce_or_message_id_keeps_uncertain(self):
  self.reconciliation_fixture();preview=self.cycle.preview_reconciliation('batch-1','s1')
  with self.assertRaisesRegex(ValueError,'review_changed'):self.cycle.confirm_reconciliation('batch-1','s1','wrong')
  with patch.object(self.mail,'message_id',return_value='<wrong@example.invalid>'):
   with self.assertRaisesRegex(ValueError,'evidence_mismatch'):self.cycle.preview_reconciliation('batch-1','s1')
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain');self.assertEqual(len(self.mail.sends),1)
 def test_reconciliation_resumes_after_ledger_commit_crash(self):
  self.reconciliation_fixture();preview=self.cycle.preview_reconciliation('batch-1','s1')
  with patch.object(self.repo,'confirm_uncertain_receipt',side_effect=RuntimeError('synthetic crash')):
   with self.assertRaises(RuntimeError):self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain')
  preview=self.cycle.preview_reconciliation('batch-1','s1')
  self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'sent');self.assertEqual(len(self.mail.sends),1)
 def test_reconciliation_receipt_conflicts_do_not_change_ledger(self):
  scenario=self.reconciliation_fixture()
  with self.repo.transaction() as con:
   con.execute("UPDATE thunder_effect_jobs SET receipt_ref='gmail:ffff' WHERE id=?",(self.job['id'],))
  preview=self.cycle.preview_reconciliation('batch-1','s1')
  with self.assertRaisesRegex(Exception,'receipt conflict'):
   self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])
  with scenario.database() as (con,_):self.assertEqual(con.execute('SELECT status,provider_id FROM toques').fetchall(),[('incerto',None)])
  with self.repo.transaction() as con:
   con.execute('UPDATE thunder_effect_jobs SET receipt_ref=NULL WHERE id=?',(self.job['id'],))
  second=self.repo.enqueue_effect('synthetic-conflicting-receipt',deepcopy(self.job['payload']))
  with self.repo.transaction() as con:
   con.execute("UPDATE thunder_effect_jobs SET state='sent',receipt_ref='email:gmail:abc123' WHERE id=?",(second['id'],))
  preview=self.cycle.preview_reconciliation('batch-1','s1')
  with self.assertRaisesRegex(Exception,'already bound'):
   self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])
  with scenario.database() as (con,_):self.assertEqual(con.execute('SELECT status,provider_id FROM toques').fetchall(),[('incerto',None)])
 def test_reconciliation_preserves_pause_and_approval(self):
  self.reconciliation_fixture();cadence=self.repo.get_state('cadence','batch-1')
  self.repo.put_state('cadence','batch-1',{**cadence,'status':'paused_reply'})
  approval=self.repo.get_state('approval','batch-1')
  preview=self.cycle.preview_reconciliation('batch-1','s1')
  self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])
  self.assertEqual(self.repo.get_state('cadence','batch-1')['status'],'paused_reply')
  self.assertEqual(self.repo.get_state('approval','batch-1'),approval)
  self.assertEqual(len(self.mail.sends),1)
 def test_concurrent_receipt_confirmations_do_not_requeue(self):
  from concurrent.futures import ThreadPoolExecutor
  self.reconciliation_fixture();preview=self.cycle.preview_reconciliation('batch-1','s1')
  def confirm():
   try:return self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])['status']
   except ValueError:return 'stale_review'
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:confirm(),range(2)))
  self.assertIn('confirmed',results)
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'sent');self.assertEqual(len(self.mail.sends),1)
 def test_terminal_ledger_receipt_cannot_be_overwritten(self):
  scenario=self.reconciliation_fixture()
  self.ledger.finish(self.job['effect_key'],'sent','gmail:ffff')
  preview=self.cycle.preview_reconciliation('batch-1','s1')
  with self.assertRaisesRegex(Exception,'scope or receipt conflict'):
   self.cycle.confirm_reconciliation('batch-1','s1',preview['review_hash'])
  with scenario.database() as (con,_):self.assertEqual(con.execute('SELECT status,provider_id FROM toques').fetchall(),[('feito','gmail:ffff')])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain')
 def test_default_off_no_credentials_or_write(self):
  with patch.dict('os.environ',{},clear=True):
   adapter=configured_adapter(self.repo);cycle=configured_cycle(self.repo)
  self.assertEqual(adapter.capability,'disabled')
  with self.assertRaisesRegex(ValueError,'disabled'):adapter.prepare_job(self.job)
  with self.assertRaisesRegex(ValueError,'disabled'):cycle.receive_reply('gmail:abc123','def456','abc124',review={})
  self.assertEqual(self.mail.sends,[])
 def test_policy_recipient_mutation_blocks_before_transport(self):
  self.policy['recipient']='other@example.invalid';self.worker.process_one()
  self.assertEqual(self.mail.sends,[]);self.assertEqual(self.ledger.calls,[])
 def test_expired_policy_blocks(self):
  self.policy['expires_at']=time.time()-1;self.worker.process_one();self.assertEqual(self.mail.sends,[])
 def test_uncertain_is_not_resent_and_reconcile_is_read_only(self):
  self.mail.fail=True;self.worker.process_one();self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain')
  self.worker.process_one();self.assertEqual(len(self.mail.sends),1)
  self.assertFalse(self.adapter.reconcile('batch-1','s1')['retry_write']);self.assertEqual(len(self.mail.sends),1)
 def test_changed_message_or_review_does_not_pause(self):
  self.sent()
  with self.assertRaises(ValueError):self.cycle.receive_reply('gmail:abc123','def456','abc124',review={'actor':'ygor','human_reply':True,'message_hash':'bad'})
  self.assertEqual(self.repo.get_state('cadence','batch-1')['status'],'approved')
 def test_crm_disabled_is_independent_of_send(self):
  self.sent();cycle=ThunderEmailCycle(self.repo,reader=self.reader,client=self.mail,crm=self.crm)
  with self.assertRaisesRegex(ValueError,'disabled'):cycle.log_email(cycle.prepare_email('gmail:abc123'))
  self.assertEqual(self.crm.writes,[])

if __name__=='__main__':unittest.main()
