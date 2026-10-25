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
  self.assisted=CodexAssisted(self.service,self.ledger,reader=self.reader,enabled=True,policy_loader=lambda:deepcopy(self.policy))
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
 def test_followup_has_no_false_threading_contract(self):
  with self.assertRaisesRegex(ValueError,'first_email_only'):self.assisted.preview('batch-1','s2')

if __name__=='__main__':unittest.main()
