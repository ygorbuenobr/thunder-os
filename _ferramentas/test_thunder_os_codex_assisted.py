"""Synthetic assisted handoff through real repository/ledger; no network or send."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from thunder_os.codex_assisted import CodexAssisted,configured_assisted
import test_thunder_os_thunder_email_runtime as email_fixtures
import test_thunder_os_eval_trajectory as ledger_fixtures

class AssistedTests(unittest.TestCase):
 def setUp(self):
  email_fixtures.ThunderEmailTests.setUp(self)
  scenario=ledger_fixtures.EvalTrajectoryTests();scenario.setUp();self.addCleanup(scenario.doCleanups)
  self.ledger=scenario.ledger;self.worker.ledger=self.ledger;self.cycle.ledger=self.ledger
  import time
  from datetime import datetime,timezone
  from email.utils import format_datetime
  fixture_now=int(time.time());self.policy.update(issued_at=fixture_now-1,expires_at=fixture_now+250)
  self.mail.date=format_datetime(datetime.fromtimestamp(fixture_now,timezone.utc))
  self.assisted=CodexAssisted(self.service,self.ledger,reader=self.reader,enabled=True,policy_loader=lambda:deepcopy(self.policy),clock=lambda:fixture_now)
  self.mail.envelope={'to':self.step['recipient'],'subject':self.step['subject'],'body':self.step['content'],'thread_id':None}
 def packet(self):return self.assisted.preview('batch-1','s1')
 def claim(self):
  ticket=self.assisted.claim('batch-1','s1',self.packet()['review_hash']);self.assisted.dispatch(ticket['job_id'],ticket['ticket_hash']);return ticket
 def test_full_chain_receipt_crm_reply_stop_without_second_send(self):
  ticket=self.claim();self.assertTrue(ticket['dispatch_authorized']);self.assertEqual(self.mail.sends,[])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain')
  self.mail.send(self.mail.envelope) # synthetic provider only, no network
  preview=self.assisted.receipt_preview(self.job['id'],'gmail:abc123')
  self.assisted.receipt_confirm(self.job['id'],'gmail:abc123',preview['review_hash'])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'sent')
  self.assertEqual(self.repo.get_state('codex_assisted',self.job['id'])['attempt_attribution'],'operator_reported')
  from thunder_os.journey_readonly import JourneyReadonly
  view=object.__new__(JourneyReadonly);view.repo=self.repo
  self.assertEqual(view._effects()[0]['assisted_evidence']['verification'],'provider_sent_copy')
  # Idempotent re-read/return, no new reservation or provider send.
  preview=self.assisted.receipt_preview(self.job['id'],'gmail:abc123')
  self.assisted.receipt_confirm(self.job['id'],'gmail:abc123',preview['review_hash'])
  plan=self.cycle.prepare_email('gmail:abc123');self.cycle.log_email(plan);self.cycle.log_email(plan)
  self.assertEqual(len(self.crm.writes),1)
  second=self.service.dispatch_step('batch-1','s2','ygor')
  reply=self.cycle.preview_reply('gmail:abc123','def456','abc124')
  self.cycle.receive_reply('gmail:abc123','def456','abc124',review={'actor':'ygor','human_reply':True,'message_hash':reply['message_hash']})
  self.worker.process_one();self.assertEqual(self.repo.effect(second['id'])['state'],'failed')
  self.assertEqual(self.repo.get_state('cadence','batch-1')['status'],'paused_reply');self.assertEqual(len(self.mail.sends),1)
 def test_reservation_crash_requires_explicit_same_identity_reconciliation(self):
  packet=self.packet();original=self.repo.append_state
  def fault(kind,*args,**kwargs):
   if kind=='codex_assisted':raise RuntimeError('synthetic crash after PostgreSQL commit')
   return original(kind,*args,**kwargs)
  with patch.object(self.repo,'append_state',side_effect=fault):
   with self.assertRaisesRegex(RuntimeError,'synthetic crash'):self.assisted.claim('batch-1','s1',packet['review_hash'])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'pending')
  self.assertIsNone(self.repo.get_state('codex_assisted',self.job['id']))
  owned=self.ledger.reservation(self.job['effect_key'],self.job['payload']['step'])
  self.assertEqual(owned['status'],'reserved')
  with self.assertRaisesRegex(ValueError,'reconciliation_required'):self.assisted.claim('batch-1','s1',packet['review_hash'])
  ticket=self.assisted.reconcile_reservation('batch-1','s1',packet['review_hash'])
  self.assertEqual(ticket['reservation_id'],owned['id'])
  self.assertEqual(ticket['job_id'],self.job['id'])
  self.assertEqual(self.mail.sends,[])
  with self.assertRaisesRegex(ValueError,'unavailable'):self.assisted.reconcile_reservation('batch-1','s1',packet['review_hash'])
 def test_reader_only_composition_never_falls_back_to_generic_writer(self):
  from thunder_os.touch_ledger import AssistedLedger,TouchBlocked
  self.assisted.ledger=AssistedLedger(self.ledger)
  self.assisted.worker.ledger=self.assisted.ledger
  with self.assertRaisesRegex(TouchBlocked,'writer_not_configured'):self.assisted.claim('batch-1','s1',self.packet()['review_hash'])
  self.assertIsNone(self.ledger.reservation(self.job['effect_key'],self.job['payload']['step']))
  self.assertEqual(self.mail.sends,[])
 def test_explicit_writer_composition_and_grant_bound_to_packet(self):
  from thunder_os.touch_ledger import AssistedLedger
  self.assisted.ledger=AssistedLedger(self.ledger,self.ledger)
  self.assisted.worker.ledger=self.assisted.ledger
  packet=self.packet();ticket=self.assisted.claim('batch-1','s1',packet['review_hash'])
  grant=self.assisted.dispatch(ticket['job_id'],ticket['ticket_hash'])
  self.assertEqual(grant['packet_hash'],packet['review_hash'])
  self.assertEqual(grant['job_id'],packet['job_id'])
  self.assertLessEqual(grant['expires_at']-grant['issued_at'],5)
 def test_foreign_duplicate_cannot_be_adopted_during_reconciliation(self):
  self.ledger.reserve('foreign-synthetic-effect','s1',self.job['payload']['step'])
  packet=self.packet()
  for invoke in (self.assisted.claim,self.assisted.reconcile_reservation):
   with self.assertRaisesRegex(ValueError,'already_reserved'):invoke('batch-1','s1',packet['review_hash'])
  self.assertIsNone(self.ledger.reservation(self.job['effect_key'],self.job['payload']['step']))
  self.assertEqual(self.mail.sends,[])
 def test_reservation_changed_before_dispatch_never_authorizes_call(self):
  ticket=self.assisted.claim('batch-1','s1',self.packet()['review_hash'])
  self.ledger.finish(self.job['effect_key'],'sent','gmail:already-synthetic')
  with self.assertRaisesRegex(ValueError,'reservation_changed'):
   self.assisted.dispatch(ticket['job_id'],ticket['ticket_hash'])
  self.assertEqual(self.mail.sends,[])
 def test_confirmation_cross_database_crash_reconciles_same_receipt_without_send(self):
  self.claim()
  preview=self.assisted.receipt_preview(self.job['id'],'gmail:abc123')
  with patch.object(self.repo,'confirm_uncertain_receipt',side_effect=RuntimeError('synthetic crash after ledger confirmation')):
   with self.assertRaises(RuntimeError):self.assisted.receipt_confirm(self.job['id'],'gmail:abc123',preview['review_hash'])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain')
  self.assertEqual(self.ledger.reservation(self.job['effect_key'],self.job['payload']['step'])['status'],'sent')
  preview=self.assisted.receipt_preview(self.job['id'],'gmail:abc123')
  result=self.assisted.receipt_confirm(self.job['id'],'gmail:abc123',preview['review_hash'])
  self.assertEqual(result['review_hash'],preview['review_hash'])
  self.assertEqual(result['status'],'confirmed')
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'sent')
  self.assertEqual(self.mail.sends,[])
 def test_authorized_writer_factory_does_not_change_global_readonly(self):
  from contextlib import contextmanager
  from unittest.mock import Mock
  from thunder_os.touch_ledger import assisted_writer_database
  con=Mock();con.cursor.return_value.fetchone.return_value=('thunder_gtm','thunder_gtm','thunder_gtm','54322','off')
  @contextmanager
  def database():yield con,'%s'
  with patch.dict('os.environ',{'PGOPTIONS':'-c default_transaction_read_only=on','THUNDER_OS_CODEX_ASSISTED_WRITER_ENABLED':'true'}):
   app=configured_assisted(self.service)
   self.assertIsNotNone(app.ledger.writer)
   with patch('thunder_os.touch_ledger.toques._banco',database):
    with assisted_writer_database() as (seen,marker):self.assertIs(seen,con)
   import os
   self.assertEqual(os.environ['PGOPTIONS'],'-c default_transaction_read_only=on')
  con.set_session.assert_called_once_with(readonly=False)
  con.cursor.return_value.fetchone.return_value=('postgres','postgres','thunder_gtm','54322','off')
  with patch('thunder_os.touch_ledger.toques._banco',database):
   with self.assertRaisesRegex(Exception,'identity_mismatch'):
    with assisted_writer_database():pass
 def test_default_off_preview_does_not_authorize(self):
  with patch.dict('os.environ',{},clear=True):app=configured_assisted(self.service)
  self.assertFalse(app.preview('batch-1','s1')['dispatch_authorized'])
  with self.assertRaisesRegex(ValueError,'disabled'):app.claim('batch-1','s1',self.packet()['review_hash'])
 def test_claim_cannot_be_reissued(self):
  p=self.packet();self.claim()
  with self.assertRaisesRegex(ValueError,'unavailable'):self.assisted.claim('batch-1','s1',p['review_hash'])
  self.assertEqual(self.mail.sends,[])
 def test_edit_invalidates_packet(self):
  p=self.packet();c=self.repo.get_state('cadence','batch-1');c['steps'][0]['content']='edited';self.repo.put_state('cadence','batch-1',c)
  with self.assertRaisesRegex(ValueError,'review_changed'):self.assisted.claim('batch-1','s1',p['review_hash'])
 def test_pause_prevents_claim(self):
  self.repo.put_state('control','global',{'paused':True})
  with self.assertRaisesRegex(ValueError,'pause'):self.claim()
 def test_wrong_content_or_historical_receipt_is_rejected(self):
  self.claim();self.mail.envelope['body']='wrong'
  with self.assertRaisesRegex(ValueError,'content_mismatch'):self.assisted.receipt_preview(self.job['id'],'gmail:abc123')
  self.mail.envelope['body']=self.step['content'];self.mail.date='Thu, 01 Jan 1970 00:00:00 +0000'
  with self.assertRaisesRegex(ValueError,'outside_attempt'):self.assisted.receipt_preview(self.job['id'],'gmail:abc123')
 def test_receipt_review_cannot_be_forged(self):
  self.claim()
  with self.assertRaisesRegex(ValueError,'review_changed'):self.assisted.receipt_confirm(self.job['id'],'gmail:abc123','0'*64)
 def test_normal_receipt_cannot_use_manual_observation_window(self):
  from datetime import datetime,timezone
  from email.utils import format_datetime
  self.claim();row=self.repo.get_state('codex_assisted',self.job['id'])
  self.mail.date=format_datetime(datetime.fromtimestamp(row['grant']['expires_at']+31,timezone.utc))
  with self.assertRaisesRegex(ValueError,'outside_attempt'):self.assisted.receipt_preview(self.job['id'],'gmail:abc123')
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain')
 def test_revocation_between_claim_and_dispatch_blocks(self):
  t=self.assisted.claim('batch-1','s1',self.packet()['review_hash'])
  self.repo.put_state('control','global',{'paused':True})
  with self.assertRaisesRegex(ValueError,'pause'):self.assisted.dispatch(t['job_id'],t['ticket_hash'])
  self.assertEqual(self.mail.sends,[])
 def test_dispatch_is_single_use(self):
  t=self.claim()
  with self.assertRaisesRegex(ValueError,'unavailable'):self.assisted.dispatch(t['job_id'],t['ticket_hash'])
 def test_real_packet_ticket_hash_matches_javascript(self):
  import json,subprocess,time
  self.assisted.clock=lambda:float(int(time.time()))
  self.assisted.adapter.clock=self.assisted.clock
  self.policy['expires_at']=float(int(time.time())+100)
  p=self.packet();t=self.claim()
  js="const {hash}=require('./scripts/codex_assisted_runner.cjs');let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',()=>{const [p,t]=JSON.parse(s);const {review_hash,...a}=p;const {ticket_hash,...b}=t;if(hash(a)!==review_hash||hash(b)!==ticket_hash)process.exit(1);});"
  subprocess.run(['node','-e',js],input=json.dumps([p,t]),text=True,check=True)
 def test_http_actor_preview_and_dispatch_off(self):
  import painel_servidor as panel
  from types import SimpleNamespace
  from urllib.parse import urlsplit
  h=object.__new__(panel.Handler);h.command='POST'
  h.server=SimpleNamespace(thunder_service=self.service,thunder_codex_assisted=self.assisted)
  h.responder=lambda status,body:(status,body);h.corpo=lambda:{'cadence_id':'batch-1','step_id':'s1'}
  h.thunder_actor=lambda:'codex'
  route='/api/os/v1/integrations/codex-assisted/'
  with self.assertRaises(panel.Erro):h.thunder_api(urlsplit(route+'preview'))
  h.thunder_actor=lambda:'ygor'
  status,p=h.thunder_api(urlsplit(route+'preview'));self.assertEqual(status,200);self.assertFalse(p['dispatch_authorized'])
  h.corpo=lambda:{'cadence_id':'batch-1','step_id':'s1','review_hash':p['review_hash']}
  with patch.dict('os.environ',{},clear=True):
   with self.assertRaises(panel.Erro):h.thunder_api(urlsplit(route+'claim'))
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'pending')
 def test_concurrent_claim_has_one_winner(self):
  from concurrent.futures import ThreadPoolExecutor
  p=self.packet()
  def attempt(_):
   try:return self.assisted.claim('batch-1','s1',p['review_hash'])['dispatch_authorized']
   except ValueError:return False
  with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sorted(pool.map(attempt,range(2))),[False,True])
 def followup(self):
  from thunder_os.email_readback import followup_body
  from thunder_os.models import payload_hash
  import time
  from datetime import datetime,timezone
  from email.utils import format_datetime
  self.mail.date=format_datetime(datetime.now(timezone.utc))
  self.claim();p=self.assisted.receipt_preview(self.job['id'],'gmail:abc123')
  self.assisted.receipt_confirm(self.job['id'],'gmail:abc123',p['review_hash'])
  e1=deepcopy(self.mail.read('abc123'));m=e1['message'];m['headers_complete']=True
  m['headers']=[{'name':'Message-ID','value':m['message_id']}]
  messages={'abc123':e1}
  self.mail.read=lambda mid:deepcopy(messages[mid])
  fixed_time=str(int(time.time()*1000))
  def thread(tid):
   rows=[]
   for mid,response in messages.items():
    m=response['message'];rows.append({'id':mid,'thread_id':tid,'from':m['from'],'to':m['to'],
      'subject':m['subject'],'cc':'','bcc':'','labelIds':['SENT'],
      'internal_date':fixed_time,'bodies':{'text':m['body']},'complete':True,'coverage':{'reasons':[]}})
   return {'mailbox':e1['mailbox'],'status':'ok','thread':{'id':tid,'messages':rows,'complete':True,'truncated':False,'coverage':{'reasons':[]}}}
  self.mail.conversation=thread
  c=self.repo.get_state('cadence','batch-1');c['steps'][1]['subject']='Re: '+self.step['subject']
  c['steps'][1]['content']='Synthetic E2 new text.\n\nNo real recipient.'
  c['steps'].append({**c['steps'][1],'id':'s3','order':3,'predecessor_id':'s2','subject':'Synthetic E3 never sent','content':'Synthetic blocked follow-up.'})
  self.repo.put_state('cadence','batch-1',c)
  email_fixtures.CampaignConfirmationTests.approve(self)
  second=self.service.dispatch_step('batch-1','s2','ygor');step=second['payload']['step']
  self.policy.update(step_id='s2',effect_key=second['effect_key'],subject=step['subject'],content_hash=payload_hash(step['content']))
  packet=self.assisted.preview('batch-1','s2');self.policy['packet_review_hash']=packet['review_hash']
  def observed():
   copy=deepcopy(e1);m=copy['message'];m.update(id='abc125',date=format_datetime(datetime.now(timezone.utc)),subject=step['subject'],body=followup_body(packet['reply_contract'],step['content']),message_id='<synthetic-e2@example.invalid>')
   m['headers']=[{'name':'Message-ID','value':m['message_id']},{'name':'In-Reply-To','value':packet['reply_contract']['rfc_message_id']},{'name':'References','value':packet['reply_contract']['rfc_message_id']}]
   messages['abc125']=copy
  return packet,second,messages,observed
 def test_e2_full_chain_exact_quote_thread_crm_and_reply_stop(self):
  packet,second,messages,observed=self.followup()
  self.assertEqual(packet['payload']['reply_message_id'],'abc123')
  ticket=self.assisted.claim('batch-1','s2',packet['review_hash']);self.assisted.dispatch(second['id'],ticket['ticket_hash'])
  observed() # synthetic provider's complete sent copy, no network
  p=self.assisted.receipt_preview(second['id'],'gmail:abc125');self.assisted.receipt_confirm(second['id'],'gmail:abc125',p['review_hash'])
  plan=self.cycle.prepare_email('gmail:abc125');self.assertIn('On ',plan['properties']['hs_email_text'])
  self.cycle.log_email(plan);self.cycle.log_email(plan);self.assertEqual(len(self.crm.writes),1)
  self.assertEqual(self.reader.readback('gmail:abc125')['content'],second['payload']['step']['content'])
  third=self.service.dispatch_step('batch-1','s3','ygor')
  prior=self.mail.conversation
  def reply(tid):
   response=prior(tid);m=deepcopy(response['thread']['messages'][-1]);m.update(id='abc126',from_=None)
   m['from']=self.step['recipient'];m['to']=response['mailbox'];m['labelIds']=['INBOX'];m['bodies']={'text':'Synthetic response: stop.'};response['thread']['messages'].append(m);return response
  self.mail.conversation=reply
  p=self.cycle.preview_reply('gmail:abc125','def456','abc126')
  self.cycle.receive_reply('gmail:abc125','def456','abc126',review={'actor':'ygor','human_reply':True,'message_hash':p['message_hash']})
  self.assertEqual(self.repo.get_state('cadence','batch-1')['status'],'paused_reply')
  self.worker.process_one();self.assertEqual(self.repo.effect(third['id'])['state'],'failed')
  self.assertEqual(self.repo.get_state('cadence','batch-1')['steps'][2]['status'],'skipped_reply')
  with self.assertRaises(ValueError):self.assisted.dispatch(second['id'],ticket['ticket_hash'])
 def test_e2_missing_packet_approval_blocks(self):
  packet,_,_,_=self.followup();self.policy.pop('packet_review_hash')
  with self.assertRaisesRegex(ValueError,'packet_not_approved'):self.assisted.claim('batch-1','s2',packet['review_hash'])
 def test_e2_response_between_claim_and_dispatch_blocks(self):
  packet,second,messages,observed=self.followup();ticket=self.assisted.claim('batch-1','s2',packet['review_hash']);observed()
  with self.assertRaisesRegex(ValueError,'thread_changed_or_response'):self.assisted.dispatch(second['id'],ticket['ticket_hash'])
 def test_e2_packet_revoked_during_final_history_read(self):
  packet,second,_,_=self.followup();ticket=self.assisted.claim('batch-1','s2',packet['review_hash'])
  original=self.mail.conversation
  def revoked(tid):
   data=original(tid);self.policy['packet_review_hash']='0'*64;return data
  self.mail.conversation=revoked
  with self.assertRaisesRegex(ValueError,'packet_not_approved'):self.assisted.dispatch(second['id'],ticket['ticket_hash'])
  self.assertEqual(self.repo.get_state('codex_assisted',second['id'])['phase'],'issued')
 def test_e2_bad_thread_headers_or_quote_never_confirm(self):
  packet,second,messages,observed=self.followup();ticket=self.assisted.claim('batch-1','s2',packet['review_hash']);self.assisted.dispatch(second['id'],ticket['ticket_hash']);observed()
  original=deepcopy(messages['abc125'])
  for change in ('thread','header','quote','new_text','duplicate_header'):
   messages['abc125']=deepcopy(original);m=messages['abc125']['message']
   if change=='thread':m['thread_id']='bad123'
   elif change=='header':m['headers'][1]['value']='<wrong@example.invalid>'
   elif change=='quote':m['body']+='\n> injected'
   elif change=='new_text':m['body']='changed'+m['body']
   else:m['headers'].append(deepcopy(m['headers'][1]))
   with self.subTest(change=change),self.assertRaises(ValueError):self.assisted.receipt_preview(second['id'],'gmail:abc125')
  self.assertEqual(self.repo.effect(second['id'])['state'],'uncertain')
 def test_e2_partial_thread_blocks_preview(self):
  _,_,_,_=self.followup();prior=self.mail.conversation
  def partial(tid):
   d=prior(tid);d['thread']['complete']=False;return d
  self.mail.conversation=partial
  with self.assertRaisesRegex(ValueError,'coverage_incomplete'):self.assisted.preview('batch-1','s2')
 def test_followup_has_no_false_threading_contract(self):
  with self.assertRaisesRegex(ValueError,'predecessor_unconfirmed'):self.assisted.preview('batch-1','s2')

class ManualReconciliationTests(unittest.TestCase):
 """Known internal pre-tool failure; real disposable repository/ledger, fake Gmail."""
 def setUp(self):
  from test_thunder_os_internal_canary import CanaryTests
  from thunder_os.codex_assisted import MANUAL_JOB_ID,MANUAL_RECIPIENT
  from thunder_os.email_readback import GmailReceiptReader
  from thunder_os.seqd_client import MAILBOX
  from datetime import datetime,timezone
  from email.utils import format_datetime
  import time
  CanaryTests.setUp(self)
  for target in ('http.client.HTTPConnection','http.client.HTTPSConnection','socket.create_connection'):
   guard=patch(target,side_effect=AssertionError('network forbidden'));guard.start();self.addCleanup(guard.stop)
  self.now=int(time.time());self.policy.update(issued_at=self.now-1,expires_at=self.now+600,recipient=MANUAL_RECIPIENT)
  self.data['scope']['to']=MANUAL_RECIPIENT
  from thunder_os.approvals import _step_hash
  self.policy['effect_key']=_step_hash({'id':'e1','recipient':MANUAL_RECIPIENT,'channel':'email',
   'subject':self.data['scope']['subject'],'content':self.data['scope']['content'],'order':1,'interval_hours':0})
  p=self.s.prepare(self.data,'ygor');c=p['cadence']
  self.s.approve_cadence(c['id'],'ygor',expected_version=c['version'],review_hash=p['review_hash'],execution_confirmation=p['execution_confirmation'])
  job=self.s.dispatch_step(c['id'],'e1','ygor')
  with self.repo.transaction() as con:con.execute('UPDATE thunder_effect_jobs SET id=? WHERE id=?',(MANUAL_JOB_ID,job['id']))
  self.job=self.repo.effect(MANUAL_JOB_ID);self.mail=email_fixtures.Mail()
  self.mail.envelope={'to':MANUAL_RECIPIENT,'subject':self.data['scope']['subject'],'body':self.data['scope']['content'],'thread_id':None}
  self.reader=GmailReceiptReader(self.repo,self.mail)
  self.assisted=CodexAssisted(self.s,self.ledger,reader=self.reader,enabled=True,policy_loader=lambda:self.policy,clock=lambda:self.now)
  self.packet=self.assisted.preview(c['id'],'e1')
  self.ticket=self.assisted.claim(c['id'],'e1',self.packet['review_hash'])
  self.grant=self.assisted.dispatch(MANUAL_JOB_ID,self.ticket['ticket_hash'])
  self.now+=10
  # Expired canary and sending disabled must not block observational readback.
  internal=self.repo.get_state('internal_canary',c['id']);internal['expires_at']=self.now-1
  self.repo.put_state('internal_canary',c['id'],internal)
  self.policy['expires_at']=self.now-1;self.assisted.enabled=False;self.assisted.adapter.enabled=False
  current=self.repo.get_state('cadence',c['id']);current['steps'][0]['status']='uncertain'
  self.repo.put_state('cadence',c['id'],current)
  self.receipt='gmail:abc123'
  self.mail.date=format_datetime(datetime.fromtimestamp(self.now+2,timezone.utc))
  self.mailbox=MAILBOX
 def proof(self,**changes):
  from thunder_os.codex_assisted import ProvenNotSentProof
  from thunder_os.models import payload_hash
  import hashlib,json
  marker={'packet_hash':self.packet['review_hash'],'ticket_hash':self.ticket['ticket_hash'],
   'job_id':self.job['id'],'not_after':self.grant['expires_at']}
  data={'job_id':self.job['id'],'packet_hash':self.packet['review_hash'],'ticket_hash':self.ticket['ticket_hash'],
   'grant_hash':payload_hash(self.grant),'tool_not_invoked':True,'durable_attempt_consumed':True,
   'host_marker_hash':hashlib.sha256((json.dumps(marker,separators=(',',':'))+'\n').encode()).hexdigest(),
   'exact_query_zero':True,'evidence_hash':payload_hash({'fixture':'pre-tool dispatch expired; exact search zero'}),
   'operator_at':self.now,'failure_code':'RPC_DISPATCH_EXPIRED','actor':'ygor'}
  return ProvenNotSentProof(**{**data,**changes})
 def reconcile(self):return self.assisted.reconcile_proven_not_sent(self.job['id'],self.proof())
 def confirm(self):
  self.reconcile();self.now+=2
  p=self.assisted.receipt_preview(self.job['id'],self.receipt)
  return self.assisted.receipt_confirm(self.job['id'],self.receipt,p['review_hash'])
 def test_proven_failure_is_terminal_audited_and_never_retryable(self):
  from thunder_os.db import RepositoryConflict
  before=self.ledger.reservation(self.job['effect_key'],self.job['payload']['step'])
  result=self.reconcile();job=self.repo.effect(self.job['id']);row=self.repo.get_state('codex_assisted',job['id'])
  self.assertEqual((job['state'],job['error_code']),('failed','proved_not_sent'))
  self.assertEqual(row['phase'],'proved_not_sent');self.assertEqual(row['ticket'],self.ticket);self.assertEqual(row['grant'],self.grant)
  self.assertEqual(self.ledger.reservation(job['effect_key'],job['payload']['step']),before)
  self.assertEqual(row['manual_reconciliation']['observe_until']-self.now,86400)
  self.assertIs(row['manual_reconciliation']['proof']['durable_attempt_consumed'],True)
  self.assertEqual(row['manual_reconciliation']['host_marker']['not_after'],self.grant['expires_at'])
  self.assertEqual([e['type'] for e in self.repo.events(job['id'])],['assisted_proved_not_sent'])
  for k in ('dispatch_authorized','send_attempted','retry_write'):self.assertIs(result[k],False)
  with self.assertRaises(RepositoryConflict):self.repo.retry_effect(job['id'])
  with self.assertRaises(RepositoryConflict):self.repo.requeue_confirmed_effect(job,'0'*64)
  with self.assertRaises(ValueError):self.assisted.dispatch(job['id'],self.ticket['ticket_hash'])
  with self.assertRaises(ValueError):self.reconcile()
  with self.repo.transaction(),self.assertRaises(RepositoryConflict):self.repo.validate_uncertain_receipt(job,self.receipt)
  self.assertEqual(self.mail.sends,[])
 def test_timeout_spoofed_mismatched_and_stale_attestations_do_not_prove_no_send(self):
  cases=[{'failure_code':'TIMEOUT'},{'failure_code':'RPC_TIMEOUT'},{'tool_not_invoked':False},
   {'tool_not_invoked':1},{'durable_attempt_consumed':False},{'durable_attempt_consumed':1},{'exact_query_zero':False},
   {'actor':'codex'},{'operator_at':self.now-301},{'operator_at':self.now+1},{'operator_at':float('nan')},
   {'packet_hash':'0'*64},{'ticket_hash':'0'*64},{'grant_hash':'0'*64},{'evidence_hash':'bad'},{'host_marker_hash':'0'*64},
   {'job_id':'effect-other'}]
  before=self.repo.effect(self.job['id'])
  for changes in cases:
   with self.subTest(changes=changes),self.assertRaises(ValueError):
    self.assisted.reconcile_proven_not_sent(self.job['id'],self.proof(**changes))
   self.assertEqual(self.repo.effect(self.job['id']),before)
  with self.assertRaises(ValueError):self.assisted.reconcile_proven_not_sent(self.job['id'],{})
  self.assertEqual(self.repo.events(self.job['id']),[])
 def test_consumed_attempt_or_lease_or_other_job_prevents_reconciliation(self):
  from thunder_os.models import payload_hash
  key=payload_hash({'mailbox':self.mailbox,'cadence':self.data['id'],'step':'e1'})
  with self.repo.transaction():
   self.repo.put_state('gmail_cadence_attempt',key,{'phase':'uncertain'})
   with self.assertRaisesRegex(ValueError,'gmail_attempt_exists'):self.reconcile()
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain')
 def test_consumed_host_marker_must_bind_exact_packet_ticket_job_and_expiry(self):
  import hashlib,json
  original={'packet_hash':self.packet['review_hash'],'ticket_hash':self.ticket['ticket_hash'],
   'job_id':self.job['id'],'not_after':self.grant['expires_at']}
  for field,bad in [('packet_hash','0'*64),('ticket_hash','0'*64),('job_id','effect-other'),
                    ('not_after',self.grant['expires_at']+1)]:
   marker={**original,field:bad}
   digest=hashlib.sha256((json.dumps(marker,separators=(',',':'))+'\n').encode()).hexdigest()
   with self.subTest(field=field),self.assertRaisesRegex(ValueError,'host_marker_mismatch'):
    self.assisted.reconcile_proven_not_sent(self.job['id'],self.proof(host_marker_hash=digest))
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'uncertain')
  self.assertEqual(self.repo.events(self.job['id']),[]);self.assertEqual(self.mail.sends,[])
 def test_live_lease_or_changed_internal_envelope_cannot_be_reconciled(self):
  with self.repo.transaction() as con:
   con.execute("UPDATE thunder_effect_jobs SET lease_until='2099-01-01' WHERE id=?",(self.job['id'],))
  with self.assertRaisesRegex(ValueError,'original_state_changed'):self.reconcile()
  with self.repo.transaction() as con:con.execute('UPDATE thunder_effect_jobs SET lease_until=NULL WHERE id=?',(self.job['id'],))
  cadence=self.repo.get_state('cadence',self.data['id']);cadence['steps'][0]['content']='mutated'
  self.repo.put_state('cadence',self.data['id'],cadence)
  with self.assertRaisesRegex(ValueError,'envelope_changed'):self.reconcile()
  self.assertEqual(self.repo.events(self.job['id']),[]);self.assertEqual(self.mail.sends,[])
 def test_manual_receipt_fresh_read_canonical_step_and_projection_with_flags_off(self):
  result=self.confirm();job=self.repo.effect(self.job['id']);row=self.repo.get_state('codex_assisted',job['id'])
  self.assertEqual(job['state'],'sent');self.assertEqual(row['phase'],'confirmed')
  self.assertEqual(row['operator_receipt_event']['receipt_ref'],self.receipt)
  self.assertEqual(row['operator_receipt_event']['lead_email'],self.data['scope']['to'])
  self.assertEqual(row['attempt_attribution'],'operator_reported');self.assertEqual(row['verification'],'provider_sent_copy')
  self.assertEqual(self.repo.get_state('cadence',self.data['id'])['steps'][0]['status'],'sent')
  from thunder_os.journey_readonly import JourneyReadonly
  view=object.__new__(JourneyReadonly);view.repo=self.repo
  self.assertEqual(view._effects()[0]['assisted_evidence']['verification'],'provider_sent_copy')
  for k in ('dispatch_authorized','send_attempted','retry_write'):self.assertIs(result[k],False)
  self.assertEqual([e['type'] for e in self.repo.events(job['id'])],['assisted_proved_not_sent','assisted_operator_receipt_confirmed'])
  p=self.assisted.receipt_preview(job['id'],self.receipt)
  self.assisted.receipt_confirm(job['id'],self.receipt,p['review_hash'])
  self.assertEqual(len(self.repo.events(job['id'])),2);self.assertEqual(self.mail.sends,[])
 def test_manual_receipt_wrong_envelope_time_and_changed_sent_copy_rejected(self):
  from datetime import datetime,timezone
  from email.utils import format_datetime
  self.reconcile();self.now+=2
  p=self.assisted.receipt_preview(self.job['id'],self.receipt)
  for field,bad in [('to','other@example.invalid'),('subject','wrong'),('body','wrong')]:
   before=self.mail.envelope[field];self.mail.envelope[field]=bad
   with self.subTest(field=field),self.assertRaises(ValueError):self.assisted.receipt_confirm(self.job['id'],self.receipt,p['review_hash'])
   self.mail.envelope[field]=before
  original=self.mail.read
  def wrong_sender(mid):
   response=original(mid);response['message']['from']='other@example.invalid';return response
  with patch.object(self.mail,'read',side_effect=wrong_sender),self.assertRaises(ValueError):
   self.assisted.receipt_preview(self.job['id'],self.receipt)
  for seconds in (-3,31,86401):
   self.mail.date=format_datetime(datetime.fromtimestamp(self.now+seconds,timezone.utc))
   with self.assertRaisesRegex(ValueError,'outside_window'):self.assisted.receipt_preview(self.job['id'],self.receipt)
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'failed');self.assertEqual(self.mail.sends,[])
 def test_manual_observation_expiry_and_spoofed_record_rejected_before_read(self):
  self.reconcile();self.now+=86401
  with patch.object(self.reader,'read_sent_event',side_effect=AssertionError('must not read')):
   with self.assertRaisesRegex(ValueError,'observation_expired'):self.assisted.receipt_preview(self.job['id'],self.receipt)
  self.now-=86401;row=self.repo.get_state('codex_assisted',self.job['id'])
  row['manual_reconciliation']['proof']['evidence_hash']='f'*64;self.repo.put_state('codex_assisted',self.job['id'],row)
  from thunder_os.db import RepositoryConflict
  with self.assertRaisesRegex(RepositoryConflict,'proof missing'):self.assisted.receipt_preview(self.job['id'],self.receipt)
 def test_manual_duplicate_receipt_and_repository_failure_have_no_partial_confirmation(self):
  from thunder_os.db import RepositoryConflict
  self.reconcile();self.now+=2
  other=self.repo.enqueue_effect('synthetic-other',{'cadence_id':'other','step':self.job['payload']['step']})
  self.repo.finish_effect(other['id'],'sent','email:'+self.receipt,None)
  p=self.assisted.receipt_preview(self.job['id'],self.receipt)
  with self.assertRaisesRegex(RepositoryConflict,'already bound'):self.assisted.receipt_confirm(self.job['id'],self.receipt,p['review_hash'])
  self.assertEqual(self.ledger.reservation(self.job['effect_key'],self.job['payload']['step'])['status'],'reserved')
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'failed')
 def test_manual_cross_database_crash_is_recoverable_without_dispatch(self):
  self.reconcile();self.now+=2;p=self.assisted.receipt_preview(self.job['id'],self.receipt)
  with patch.object(self.repo,'confirm_operator_receipt',side_effect=RuntimeError('synthetic crash')):
   with self.assertRaises(RuntimeError):self.assisted.receipt_confirm(self.job['id'],self.receipt,p['review_hash'])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'failed')
  self.assertEqual(self.ledger.reservation(self.job['effect_key'],self.job['payload']['step'])['status'],'sent')
  p=self.assisted.receipt_preview(self.job['id'],self.receipt)
  self.assisted.receipt_confirm(self.job['id'],self.receipt,p['review_hash'])
  self.assertEqual(self.repo.effect(self.job['id'])['state'],'sent');self.assertEqual(self.mail.sends,[])
 def reply_fixture(self):
  self.confirm();original=self.mail.read
  def complete(mid):
   response=original(mid);m=response['message'];m.update(headers_complete=True,headers=[{'name':'Message-ID','value':m['message_id']}]);return response
  self.mail.read=complete
  def thread(tid):
   def message(mid,outgoing):
    return {'id':mid,'thread_id':tid,'from':self.mailbox if outgoing else self.data['scope']['to'],
     'to':self.data['scope']['to'] if outgoing else self.mailbox,'cc':'','bcc':'',
     'labelIds':['SENT'] if outgoing else ['INBOX'],'internal_date':str((self.now+(0 if outgoing else 1))*1000),
     'subject':self.data['scope']['subject'],'bodies':{'text':self.data['scope']['content'] if outgoing else 'Recebido'},
     'complete':True,'coverage':{'reasons':[]}}
   return {'mailbox':self.mailbox,'status':'ok','thread':{'id':tid,'messages':[message('abc123',True),message('abc124',False)]}}
  self.mail.conversation=thread
 def test_scoped_human_reply_pauses_internal_only_without_global_flags_crm_or_send(self):
  from thunder_os.thunder_email_runtime import configured_cycle
  import os
  self.reply_fixture()
  with patch.dict(os.environ,{},clear=True):
   global_cycle=configured_cycle(self.repo);self.assertFalse(global_cycle.reply_enabled)
   p=self.assisted.manual_reply_preview(self.job['id'],self.receipt,'def456','abc124')
   self.assertEqual(p['message']['text'],'Recebido')
   review={'actor':'ygor','human_reply':True,'message_hash':p['message_hash']}
   result=self.assisted.manual_reply_confirm(self.job['id'],self.receipt,'def456','abc124',review)
   self.assertEqual(result['status'],'paused_internal');self.assertEqual(result['cadence_readback']['status'],'paused_reply')
   self.assertEqual(result['external_writes'],0);self.assertFalse(global_cycle.reply_enabled)
   for k in ('dispatch_authorized','send_attempted','retry_write'):self.assertIs(result[k],False)
   duplicate=self.assisted.manual_reply_confirm(self.job['id'],self.receipt,'def456','abc124',review)
   self.assertTrue(duplicate['duplicate']);self.assertEqual(len(self.repo.list_state('reply')),1)
  self.assertEqual(self.mail.sends,[])
 def test_scoped_reply_rejects_unconfirmed_wrong_job_thread_message_and_review(self):
  with self.assertRaisesRegex(ValueError,'confirmed_receipt_required'):
   self.assisted.manual_reply_preview(self.job['id'],self.receipt,'def456','abc124')
  self.reply_fixture()
  for job,receipt,thread,message in [(self.job['id'],self.receipt,'fff999','abc124'),('effect-other',self.receipt,'def456','abc124'),
    (self.job['id'],'gmail:abc125','def456','abc124'),(self.job['id'],self.receipt,'def456','abc129')]:
   with self.assertRaises(ValueError):self.assisted.manual_reply_preview(job,receipt,thread,message)
  for review in (None,{'actor':'codex','human_reply':True,'message_hash':'0'*64},
    {'actor':'ygor','human_reply':False,'message_hash':'0'*64},{'actor':'ygor','human_reply':True,'message_hash':'0'*64}):
   with self.assertRaises(ValueError):self.assisted.manual_reply_confirm(self.job['id'],self.receipt,'def456','abc124',review)
  self.assertEqual(self.repo.list_state('reply'),[]);self.assertEqual(self.mail.sends,[])
 def test_scoped_reply_wrong_sender_and_scope_change_during_final_read_are_blocked(self):
  self.reply_fixture();original=self.mail.conversation
  def wrong(tid):
   response=original(tid);response['thread']['messages'][1]['from']='other@example.invalid';return response
  with patch.object(self.mail,'conversation',side_effect=wrong),self.assertRaises(ValueError):
   self.assisted.manual_reply_preview(self.job['id'],self.receipt,'def456','abc124')
  preview=self.assisted.manual_reply_preview(self.job['id'],self.receipt,'def456','abc124')
  calls=[]
  def changed(tid):
   response=original(tid);calls.append(tid)
   if len(calls)==2:
    row=self.repo.get_state('codex_assisted',self.job['id']);row['manual_reconciliation']['proof']['evidence_hash']='b'*64
    self.repo.put_state('codex_assisted',self.job['id'],row)
   return response
  review={'actor':'ygor','human_reply':True,'message_hash':preview['message_hash']}
  with patch.object(self.mail,'conversation',side_effect=changed),self.assertRaisesRegex(ValueError,'manual_reply_binding'):
   self.assisted.manual_reply_confirm(self.job['id'],self.receipt,'def456','abc124',review)
  self.assertEqual(self.repo.list_state('reply'),[])
  self.assertEqual(self.repo.get_state('cadence',self.data['id'])['status'],'approved')

if __name__=='__main__':unittest.main()
