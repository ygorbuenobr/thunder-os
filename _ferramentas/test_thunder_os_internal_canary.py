"""Synthetic one-email tests in disposable DB; no network or commercial data."""
from pathlib import Path
import tempfile,time,unittest
from unittest.mock import patch
from thunder_os.db import Repository
from thunder_os.service import ThunderService
from thunder_os.internal_canary import InternalCanaryService
from thunder_os.codex_assisted import CodexAssisted
from thunder_os.models import payload_hash
from thunder_os.approvals import _step_hash
from thunder_os.seqd_client import MAILBOX
import test_thunder_os_eval_trajectory as fixtures

class CanaryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.repo=Repository(Path(self.tmp.name)/'canary.db')
  f=fixtures.EvalTrajectoryTests();f.setUp();self.addCleanup(f.doCleanups)
  self.ledger=f.ledger;self.base=ThunderService(self.repo,self.ledger);self.s=InternalCanaryService(self.base)
  self.data={'id':'internal-canary-synthetic','approval_ref':'synthetic-only','scope':{'kind':'internal_canary','from':MAILBOX,'to':'fixture@example.invalid','subject':'Synthetic internal test','content':'Synthetic, never send','max_attempts':1,'no_followup':True}}
  scope=self.data['scope'];step={'id':'e1','recipient':scope['to'],'channel':'email','subject':scope['subject'],'content':scope['content'],'order':1,'interval_hours':0}
  self.policy={'version':1,'mailbox':MAILBOX,'campaign_id':self.data['id'],'cadence_id':self.data['id'],'step_id':'e1','recipient':scope['to'],'subject':scope['subject'],'content_hash':payload_hash(scope['content']),'effect_key':_step_hash(step),'approval_ref':'synthetic-only','max_attempts':1,'issued_at':time.time()-1,'expires_at':time.time()+600}
  self.s.policy_loader=lambda:self.policy
 def test_prepare_approve_queue_claim_reuses_existing_gates_without_crm_or_send(self):
  p=self.s.prepare(self.data,'ygor');self.assertFalse(p['send_attempted']);self.assertEqual(self.repo.effects(),[])
  c=p['cadence'];self.assertNotIn('hubspot_contact_id',c)
  self.s.approve_cadence(c['id'],'ygor',expected_version=c['version'],review_hash=p['review_hash'],execution_confirmation=p['execution_confirmation'])
  job=self.s.dispatch_step(c['id'],'e1','ygor')
  policy={'version':1,'approval_ref':'synthetic-only','mailbox':MAILBOX,'campaign_id':c['id'],'cadence_id':c['id'],'step_id':'e1','effect_key':job['effect_key'],'recipient':self.data['scope']['to'],'subject':self.data['scope']['subject'],'content_hash':payload_hash(self.data['scope']['content']),'issued_at':time.time()-1,'expires_at':time.time()+60,'max_attempts':1}
  a=CodexAssisted(self.s,self.ledger,enabled=True,policy_loader=lambda:policy)
  packet=a.preview(c['id'],'e1');ticket=a.claim(c['id'],'e1',packet['review_hash']);self.assertTrue(ticket['dispatch_authorized'])
  self.assertEqual(self.repo.effect(job['id'])['state'],'uncertain')
  with self.assertRaises(ValueError):a.claim(c['id'],'e1',packet['review_hash'])
  with self.assertRaises(ValueError):self.base.current_research(c['id'])
 def test_default_off_never_claims(self):
  p=self.s.prepare(self.data,'ygor');a=CodexAssisted(self.s,self.ledger)
  with self.assertRaisesRegex(ValueError,'disabled'):a.claim(p['cadence']['id'],'e1','a'*64)
 def test_scope_change_holds(self):
  c=self.s.prepare(self.data,'ygor')['cadence'];c['steps'][0]['recipient']='other@example.invalid'
  self.assertEqual(self.s.identity_execution_error(c),'internal_canary_envelope_changed')
  c['steps'].append(dict(c['steps'][0]));self.assertEqual(self.s.identity_execution_error(c),'internal_canary_identity_changed')
 def test_human_authority_expiry_replay_and_extra_recipient(self):
  with self.assertRaises(ValueError):self.s.prepare(self.data,'codex')
  self.data['scope']['cc']='other@example.invalid'
  with self.assertRaises(ValueError):self.s.prepare(self.data,'ygor')
  del self.data['scope']['cc'];self.s.prepare(self.data,'ygor')
  with self.assertRaises(ValueError):self.s.prepare(self.data,'ygor')
  with patch('thunder_os.internal_canary.time.time',return_value=time.time()+901):
   with self.assertRaises(ValueError):self.s.current_research(self.data['id'])

 def test_other_recipient_or_copy_cannot_enter_exception(self):
  self.data['scope']['to']='other@example.invalid'
  with self.assertRaisesRegex(ValueError,'pinned_policy'):self.s.prepare(self.data,'ygor')
  self.assertEqual(self.repo.list_state('cadence'),[])
  self.assertEqual(self.repo.list_state('internal_canary'),[])
 def test_missing_policy_fails_closed(self):
  s=InternalCanaryService(self.base,policy_loader=lambda:{})
  with self.assertRaisesRegex(ValueError,'pinned_policy'):s.prepare(self.data,'ygor')
 def recovery_fixture(self):
  p=self.s.prepare(self.data,'ygor');c=p['cadence']
  self.s.approve_cadence(c['id'],'ygor',expected_version=c['version'],review_hash=p['review_hash'],execution_confirmation=p['execution_confirmation'])
  job=self.s.dispatch_step(c['id'],'e1','ygor')
  a=CodexAssisted(self.s,self.ledger,enabled=True,policy_loader=lambda:self.policy)
  packet=a.preview(c['id'],'e1')
  row=self.repo.get_state('internal_canary',c['id']);row={k:v for k,v in row.items() if k not in {'id','version','updated_at'}}
  row['expires_at']=time.time()-10;self.repo.put_state('internal_canary',c['id'],row)
  self.policy['issued_at']=time.time()-1;self.policy['expires_at']=time.time()+600
  return a,packet,job
 def test_expired_preclaim_recovery_preserves_job_then_claims_once(self):
  a,p,j=self.recovery_fixture();before=self.repo.effect(j['id'])
  proof=a.recover_preclaim(p['cadence_id'],'e1',p['review_hash'])
  self.assertTrue(proof['recovered_preclaim']);self.assertFalse(proof['dispatch_authorized'])
  self.assertEqual(self.repo.effect(j['id']),before)
  self.assertEqual(a.preview(p['cadence_id'],'e1'),p)
  with self.assertRaisesRegex(ValueError,'already_used'):a.recover_preclaim(p['cadence_id'],'e1',p['review_hash'])
  self.assertTrue(a.claim(p['cadence_id'],'e1',p['review_hash'])['dispatch_authorized'])
 def test_recovery_rejects_attempt_and_keeps_expiry(self):
  a,p,j=self.recovery_fixture();before=self.repo.get_state('internal_canary',p['cadence_id'])
  self.repo.put_state('codex_assisted',j['id'],{'phase':'issued'})
  with self.assertRaisesRegex(ValueError,'attempt_exists'):a.recover_preclaim(p['cadence_id'],'e1',p['review_hash'])
  self.assertEqual(self.repo.get_state('internal_canary',p['cadence_id']),before)
 def test_recovery_wrong_hash_never_renews(self):
  a,p,j=self.recovery_fixture();before=self.repo.get_state('internal_canary',p['cadence_id'])
  with self.assertRaisesRegex(ValueError,'review_changed'):a.recover_preclaim(p['cadence_id'],'e1','0'*64)
  self.assertEqual(self.repo.get_state('internal_canary',p['cadence_id']),before)
