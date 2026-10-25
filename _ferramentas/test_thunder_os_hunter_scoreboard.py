"""Synthetic Hunter evidence; no credentials, network, real commercial mutations."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit
from thunder_os.hunter_scoreboard import HunterScoreboard, OWNER_ID, PIPELINE, QUALIFIED, WON
from thunder_os.cadence_readonly import CadenceReadonlyError
from thunder_os.campaign_scoreboard import CampaignScoreboard

NOW = datetime(2026,10,25,tzinfo=timezone.utc)
OLD='2026-09-01T00:00:00Z'; EVENT='2026-10-10T00:00:00Z'
def history(value, when=OLD):return [{'value':value,'timestamp':when}]
def deal():
 return {'id':'1','archived':False,'properties':{'hubspot_owner_id':OWNER_ID,'pipeline':PIPELINE,'dealstage':WON},
  'propertiesWithHistory':{'hubspot_owner_id':history(OWNER_ID),'pipeline':history(PIPELINE),
   'thunder_campaign_id':history('BR-APOLLO-01'), 'dealstage':history('initial')+history(QUALIFIED,EVENT)+history(WON,'2026-10-15T00:00:00Z')},
  'associations':{'contacts':{'results':[{'id':'100'}]}}}
def meeting():
 return {'id':'2','archived':False,'properties':{'hubspot_owner_id':OWNER_ID,'hs_meeting_start_time':EVENT,
  'hs_createdate':'2026-10-01T00:00:00Z','hs_meeting_outcome':'COMPLETED'},
  'propertiesWithHistory':{'hubspot_owner_id':history(OWNER_ID),
   'hs_meeting_outcome':history('SCHEDULED','2026-10-01T00:00:00Z')+history('COMPLETED',EVENT)},
  'associations':{'contacts':{'results':[{'id':'100'}]},'deals':{'results':[{'id':'1'}]}}}
def task(person='100',message='m1'):
 return {'id':'task-'+message,'link_status':'linked','contact_ids':[person],'outcome':'sent_with_receipt',
  'receipt':{'provider':'apollo','account_id':'synthetic-account','message_id':message,'contact_id':person,
   'campaign_id':'BR-APOLLO-01','verification':'recorded_confirmation','confirmed_at':'2026-10-02T00:00:00Z'}}
def reply(person='100'):
 return {'hubspot_contact_id':person,'campaign_id':'BR-APOLLO-01','identity_confirmed':True,'human_reply':True,
  'received_at':'2026-10-05T00:00:00Z','classification':'positive',
  'qualification':{'external_recurring_partnership':True,'verified_by':'ygor','source_ref':'synthetic-human-review'},
  'source':{'provider':'apollo','account_id':'synthetic-account','provider_message_id':'reply1'}}
class Session:
 def __init__(self):self.deals=[deal()];self.meetings=[meeting()];self.calls=[];self.fail=None;self.more=False
 def identity(self):pass
 def call(self,method,path,body=None):
  assert method=='GET' and body is None or method=='POST' and path in ('/crm/v3/objects/deals/search','/crm/v3/objects/deals/batch/read','/crm/v3/objects/meetings/search','/crm/v3/objects/meetings/batch/read')
  self.calls.append(path)
  if self.fail and self.fail in path:raise CadenceReadonlyError('provider_http_error',403)
  if path=='/crm/v3/objects/deals/search':
   assert body['filterGroups'][0]['filters'][0]['value']==PIPELINE
   assert body['filterGroups'][0]['filters'][1]['value']==OWNER_ID
   return {'total':len(self.deals),'results':[{'id':r['id']} for r in self.deals],**({'paging':{'next':{'after':'next'}}} if self.more else {})}
  if path=='/crm/v3/objects/deals/batch/read':return {'results':deepcopy(self.deals)}
  if path=='/crm/v3/objects/meetings/search':
   assert body['filterGroups'][0]['filters'][0]['value']==OWNER_ID
   return {'total':len(self.meetings),'results':[{'id':r['id']} for r in self.meetings]}
  if path=='/crm/v3/objects/meetings/batch/read':return {'results':deepcopy(self.meetings)}
  if path=='/crm/v3/pipelines/deals':return {'results':[{'id':PIPELINE,'stages':[{'id':QUALIFIED},{'id':WON}]}]}
  kind=urlsplit(path).path.split('/')[-1]
  if kind in ('deals','meetings'):
   return {'results':deepcopy(getattr(self,kind)),**({'paging':{'next':{'after':'next'}}} if self.more and kind=='deals' else {})}
  ident=kind
  return {'id':ident,'archived':False,'properties':{'hubspot_owner_id':OWNER_ID},'associations':{'deals':{'results':[{'id':'1'}]}}}
class HunterTests(unittest.TestCase):
 def setUp(self):
  self.session=Session();self.repo=Mock()
  self.collector=HunterScoreboard(SimpleNamespace(session=lambda:self.session),self.repo,clock=lambda:NOW,max_pages=2)
  self.snapshot={'campaigns':[{'id':'BR-APOLLO-01','tasks':[task()]}]};self.replies=[reply()]
 def run_snapshot(self):return self.collector.snapshot(self.snapshot,self.replies)
 def test_complete_evidence_and_scope(self):
  out=self.run_snapshot()
  self.assertEqual(out['metrics']['opportunities']['value'],1);self.assertEqual(out['metrics']['won']['value'],1)
  self.assertEqual(out['metrics']['meetings_held']['value'],1);self.assertEqual(out['metrics']['reached']['value'],1)
  self.assertEqual(out['rates']['qualified_positive']['value'],1)
  self.assertIsNone(out['costs']['value']);self.assertFalse(out['complete']);self.repo.assert_not_called()
 def test_current_stage_is_not_event(self):
  self.session.deals[0]['propertiesWithHistory'].pop('dealstage')
  out=self.run_snapshot();self.assertIsNone(out['metrics']['won']['value']);self.assertFalse(out['metrics']['opportunities']['complete'])
 def test_reentry_counts_first_only(self):
  self.session.deals[0]['propertiesWithHistory']['dealstage']=history(QUALIFIED)+history('other',EVENT)+history(QUALIFIED,'2026-10-20T00:00:00Z')
  self.assertEqual(self.run_snapshot()['metrics']['opportunities']['value'],0)
 def test_malformed_history_entry_is_not_silently_dropped(self):
  self.session.deals[0]['propertiesWithHistory']['dealstage'].append('malformed')
  out=self.run_snapshot();self.assertIsNone(out['metrics']['opportunities']['value'])
  self.assertFalse(out['metrics']['opportunities']['complete'])
 def test_structured_history_value_cannot_become_complete_zero(self):
  self.session.deals[0]['propertiesWithHistory']['dealstage']=[{'timestamp':EVENT,'value':{}}]
  self.assertIsNone(self.run_snapshot()['metrics']['opportunities']['value'])
 def test_stage_conflict_rejected(self):
  self.session.deals[0]['propertiesWithHistory']['dealstage']+=history('conflict',EVENT)
  out=self.run_snapshot();self.assertIsNone(out['metrics']['opportunities']['value'])
 def test_pipeline_and_owner_not_mixed(self):
  self.session.deals[0]['properties']['pipeline']='farmer'
  out=self.run_snapshot();self.assertEqual(out['metrics']['opportunities']['value'],0)
  self.session.deals[0]['properties']['pipeline']=PIPELINE;self.session.deals[0]['properties']['hubspot_owner_id']='other'
  self.assertEqual(self.run_snapshot()['metrics']['won']['value'],0)
 def test_historical_owner_required(self):
  self.session.deals[0]['propertiesWithHistory'].pop('hubspot_owner_id')
  self.assertIsNone(self.run_snapshot()['metrics']['opportunities']['value'])
 def test_current_campaign_cannot_attribute_past_event(self):
  d=self.session.deals[0];d['properties']['thunder_campaign_id']='BR-APOLLO-01';d['propertiesWithHistory'].pop('thunder_campaign_id')
  out=self.run_snapshot();self.assertEqual(out['unattributed']['won']['value'],1)
  self.assertEqual(out['campaigns'][0]['events']['won']['value'],0)
 def test_missing_scope_prevents_complete_zero(self):
  self.session.deals=[{'id':'1','archived':False}]
  self.assertIsNone(self.run_snapshot()['metrics']['opportunities']['value'])
 def test_meeting_missing_time_is_unknown(self):
  self.session.meetings[0]['properties'].pop('hs_meeting_start_time')
  self.assertIsNone(self.run_snapshot()['metrics']['meetings_held']['value'])
 def test_meeting_creation_does_not_replace_scheduled_history(self):
  self.session.meetings[0]['propertiesWithHistory'].pop('hs_meeting_outcome')
  self.assertIsNone(self.run_snapshot()['metrics']['meetings_scheduled']['value'])
 def test_future_meeting_is_scheduled_not_held(self):
  self.session.meetings[0]['properties'].update(hs_meeting_start_time='2026-11-01T00:00:00Z',hs_meeting_outcome='SCHEDULED')
  out=self.run_snapshot();self.assertEqual(out['metrics']['meetings_scheduled']['value'],1);self.assertIsNone(out['metrics']['meetings_held']['value']);self.assertEqual(out['metrics']['meetings_held']['observed_value'],0)
 def test_duplicate_list_is_partial_not_double(self):
  self.session.meetings.append(meeting())
  out=self.run_snapshot();self.assertFalse(out['metrics']['meetings_held']['complete']);self.assertEqual(out['metrics']['meetings_held']['observed_value'],0)
 def test_filtered_search_proves_current_scope_only(self):
  self.session.meetings[0]['associations']['deals']['paging']={'next':{'after':'2'}}
  self.assertEqual(self.run_snapshot()['unattributed']['meetings_held']['value'],1)
 def test_read_denial_unknown_no_retry(self):
  self.session.fail='/objects/meetings/search'
  out=self.run_snapshot();self.assertIsNone(out['metrics']['meetings_held']['value'])
  self.assertEqual(sum('/objects/meetings/search' in p for p in self.session.calls),1)
  self.assertIsNone(out['campaigns'][0]['events']['meetings_held']['value'])
 def test_positive_requires_playbook_evidence(self):
  self.replies[0].pop('qualification');out=self.run_snapshot()
  self.assertIsNone(out['metrics']['positive']['value']);self.assertIsNone(out['rates']['qualified_positive']['value'])
 def test_ooo_not_human(self):
  self.replies[0]['human_reply']=False
  self.assertIsNone(self.run_snapshot()['metrics']['responded']['value'])
 def test_conflicting_reply_is_removed_from_all_people(self):
  self.snapshot['campaigns'][0]['tasks'].append(task('101','m2'));self.replies.append(reply('101'))
  out=self.run_snapshot();self.assertIsNone(out['metrics']['responded']['value']);self.assertIsNone(out['metrics']['positive']['value'])
 def test_receipts_from_farmer_contacts_do_not_enter_hunter(self):
  self.session.deals[0]['properties']['pipeline']='farmer'
  self.assertIsNone(self.run_snapshot()['metrics']['reached']['value'])
 def test_reply_and_receipt_duplicates_dedup(self):
  self.snapshot['campaigns'][0]['tasks'].append(task());self.replies.append(reply())
  out=self.run_snapshot();self.assertEqual(out['metrics']['reached']['value'],1);self.assertEqual(out['metrics']['responded']['value'],1)
 def test_first_touch_campaign_tie_unattributed(self):
  other=task(message='m2');other['receipt']['campaign_id']='BR-INSTANTLY-01'
  self.snapshot['campaigns'].append({'id':'BR-INSTANTLY-01','tasks':[other]})
  out=self.run_snapshot();self.assertEqual(out['unattributed']['reached_observed']['value'],1);self.assertIsNone(out['metrics']['responded']['value'])
 def test_contact_failures_count_toward_cap(self):
  self.snapshot['campaigns'][0]['tasks']=[task(str(100+i),'m'+str(i)) for i in range(60)]
  self.session.fail='/objects/contacts/'
  out=self.run_snapshot();self.assertEqual(sum('/objects/contacts/' in p for p in self.session.calls),50)
  self.assertIsNone(out['metrics']['reached']['value'])
 def test_page_limit_explicit(self):
  self.session.more=True
  out=self.run_snapshot();self.assertFalse(out['sources']['deals']['complete'])
 def test_current_meeting_association_never_backdates_campaign(self):
  out=self.run_snapshot();self.assertEqual(out['unattributed']['meetings_held']['value'],1)
  self.assertIsNone(out['campaigns'][0]['events']['meetings_held']['value'])
  self.assertFalse(out['metrics']['meetings_held']['complete'])
 def test_oversized_page_is_not_complete(self):
  self.session.deals=[{**deal(),'id':str(i+1)} for i in range(51)]
  out=self.run_snapshot();self.assertFalse(out['sources']['deals']['complete'])
  self.assertIsNone(out['metrics']['won']['value'])
 def test_search_total_mismatch_cannot_prove_zero(self):
  original=self.session.call
  def call(method,path,body=None):
   if path.endswith('/deals/search'):return {'total':12,'results':[]}
   return original(method,path,body)
  self.session.call=call
  out=self.run_snapshot();self.assertFalse(out['sources']['deals']['complete'])
  self.assertIsNone(out['metrics']['opportunities']['value'])
 def test_search_total_required(self):
  original=self.session.call
  def call(method,path,body=None):
   result=original(method,path,body)
   if path.endswith('/deals/search'):result.pop('total')
   return result
  self.session.call=call
  self.assertIsNone(self.run_snapshot()['metrics']['won']['value'])
 def test_campaign_scoreboard_composition(self):
  reader=Mock();reader.snapshot.return_value=self.snapshot
  self.repo.list_state.return_value=self.replies
  out=CampaignScoreboard(reader,self.repo,hunter=self.collector).snapshot()
  self.assertIn('hunter',out);self.assertTrue(out['read_only'])

if __name__=='__main__':unittest.main()

class ResumeTests(unittest.TestCase):
 def setUp(self):
  self.session=Session();self.session.meetings=[]
  original=self.session.call
  def call(method,path,body=None):
   if path.endswith('/deals/search'):
    self.session.calls.append(path)
    offset=int(body.get('after',0)); end=min(offset+50,301)
    return {'total':301,'results':[{'id':str(i+1)} for i in range(offset,end)],**({'paging':{'next':{'after':str(end)}}} if end<301 else {})}
   if path.endswith('/deals/batch/read'):
    self.session.calls.append(path)
    return {'results':[{**deal(),'id':r['id']} for r in body['inputs']]}
   return original(method,path,body)
  self.session.call=call
  self.collector=HunterScoreboard(SimpleNamespace(session=lambda:self.session),Mock(),clock=lambda:NOW,max_pages=1)
 def test_resume_beyond_250_and_replay(self):
  out=self.collector.snapshot({'campaigns':[]},[]); first=out['progress']['snapshot_id']; period=out['period']; turns=0
  while out['progress']['can_continue']:
   token=out['progress']['cursor']; next_out=self.collector.continue_snapshot(token); count=len(self.session.calls)
   self.assertEqual(next_out,self.collector.continue_snapshot(token));self.assertEqual(count,len(self.session.calls))
   out=next_out; turns+=1
   self.assertEqual(first,out['progress']['snapshot_id']);self.assertEqual(period,out['period'])
   self.assertLess(turns,20)
  self.assertTrue(out['progress']['enumeration_finished']);self.assertEqual(out['sources']['deals']['records_read'],301)
  self.assertEqual(out['metrics']['won']['value'],301)
 def test_invalid_and_expired_cursor(self):
  with self.assertRaises(ValueError):self.collector.continue_snapshot('invalid')
  out=self.collector.snapshot({'campaigns':[]},[]);self.collector._contexts[0]['touched']-=901
  with self.assertRaises(ValueError):self.collector.continue_snapshot(out['progress']['cursor'])
 def test_continuation_does_not_reload_legacy_reader(self):
  reader=Mock();repo=Mock();repo.list_state.return_value=[];reader.snapshot.return_value={'campaigns':[]}
  board=CampaignScoreboard(reader,repo,hunter=self.collector);out=board.snapshot()
  board.snapshot(hunter_cursor=out['hunter']['progress']['cursor']);self.assertEqual(reader.snapshot.call_count,1)

class CleanupTests(unittest.TestCase):
 def test_unexpected_initial_failure_releases_capacity(self):
  collector=HunterScoreboard(Mock(),Mock());collector._collect_snapshot=Mock(side_effect=RuntimeError('synthetic'))
  for _ in range(6):
   with self.assertRaises(RuntimeError):collector.snapshot({},[])
  self.assertEqual(collector._contexts,[]);self.assertEqual(collector._tokens,{})
 def test_busy_has_bounded_wait(self):
  import threading,time
  collector=HunterScoreboard(Mock(),Mock());locked=threading.Event();release=threading.Event()
  def hold():
   with collector._lock:locked.set();release.wait(2)
  worker=threading.Thread(target=hold);worker.start();locked.wait();started=time.monotonic()
  try:
   with self.assertRaisesRegex(ValueError,'hunter_busy'):collector.snapshot({},[])
   self.assertLess(time.monotonic()-started,0.8)
  finally:release.set();worker.join()

class DeadlineClassificationTests(unittest.TestCase):
 def test_transport_timeout_at_deadline_is_resumable_budget(self):
  from thunder_os.cadence_readonly import HubSpotReadonlyAdapter
  clock=[0];budget=Mock()
  def exhausted(*args):clock[0]=8;raise TimeoutError('synthetic transport timeout')
  budget.call.side_effect=exhausted
  session=HubSpotReadonlyAdapter(lambda:'synthetic-token',monotonic=lambda:clock[0],budget_seconds=8,read_budget=budget).session()
  with self.assertRaisesRegex(CadenceReadonlyError,'read_budget_exhausted'):session.call('GET','/crm/v3/pipelines/deals')
 def test_non_deadline_transport_error_remains_terminal(self):
  from thunder_os.cadence_readonly import HubSpotReadonlyAdapter
  budget=Mock();budget.call.side_effect=RuntimeError('synthetic')
  session=HubSpotReadonlyAdapter(lambda:'synthetic-token',monotonic=lambda:0,budget_seconds=8,read_budget=budget).session()
  with self.assertRaisesRegex(CadenceReadonlyError,'provider_read_failed'):session.call('GET','/crm/v3/pipelines/deals')

 def test_explicit_denial_at_deadline_is_never_resumable(self):
  from thunder_os.cadence_readonly import HubSpotReadonlyAdapter
  for status in (401,403,429):
   clock=[0];budget=Mock()
   def denied(*args):clock[0]=8;return status,b'{}'
   budget.call.side_effect=denied
   session=HubSpotReadonlyAdapter(lambda:'synthetic-token',monotonic=lambda:clock[0],budget_seconds=8,read_budget=budget).session()
   with self.assertRaises(CadenceReadonlyError) as caught:session.call('GET','/crm/v3/pipelines/deals')
   self.assertEqual(caught.exception.code,'provider_http_error');self.assertEqual(caught.exception.status,status)
