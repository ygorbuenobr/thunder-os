"""Synthetic provider reads and temporary SQLite only; no runtime fixtures."""
import copy
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.connectsafely_application import ConnectSafelyApplication
from thunder_os.connectsafely_client import ConnectSafelyClient, ConnectSafelyError as ProviderError
from thunder_os.connectsafely_console import ConnectSafelyConsole, ConnectSafelyError
from thunder_os.connectsafely_ledger import ConnectSafelyLedger
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.campaign_registry import CampaignRegistry
from test_campaign_sheet_support import make_sheet
from thunder_os.models import payload_hash
from thunder_os.touch_ledger import TouchBlocked

ACCOUNT = 'a' * 24
PUBLIC = 'fixture-owner'
OWNER = 'OWNER'
RECIPIENT = 'urn:li:fsd_profile:RECIPIENT'
THREAD = 'urn:li:msg_conversation:(urn:li:fsd_profile:OWNER,THREAD)'
NOW = 1800000000
PLAN = dict(account_id=ACCOUNT, account_public_id=PUBLIC, action='dm',
            recipient_public_id='fixture-target', recipient_profile_urn=RECIPIENT,
            channel='linkedin', messaging_channel='linkedin_inbox',
            content='Synthetic exact message', conversation_urn=THREAD)
MESSAGE = dict(messageUrn='urn:li:msg_message:(urn:li:fsd_profile:OWNER,MESSAGE)',
               text=PLAN['content'], sentAt=NOW*1000, senderProfileId=OWNER,
               sender=dict(profileId=OWNER, profileUrl='https://www.linkedin.com/in/'+PUBLIC),
               isSentByOwner=True)
RECEIPT = 'connectsafely:' + MESSAGE['messageUrn']


class ReadFixture:
    def __init__(self, pin=PUBLIC):
        self.calls = []
        self.exists = dict(success=True, accountId=ACCOUNT, profileUrn=RECIPIENT,
                           hasConversation=True, conversationUrn=THREAD)
        self.page = dict(success=True, accountId=ACCOUNT, conversationUrn=THREAD,
                        messages=[copy.deepcopy(MESSAGE)], hasMore=False, fetchingMore=False,
                        participants=[dict(profileId=PUBLIC), dict(profileId='fixture-target'),
                                      dict(profileId='RECIPIENT')])
        self.pages = None
        self.client = ConnectSafelyClient(ACCOUNT, PUBLIC, request=self.request,
                                         clock=lambda:NOW+30, self_profile_id=pin)
    def request(self, method, path, query, body):
        if method != 'GET':
            raise AssertionError('reconciliation is read-only')
        self.calls.append((method, path, query, body))
        if path.endswith('/accounts'):
            return 200, [dict(id=ACCOUNT, publicId=PUBLIC, status='AVAILABLE', enabled=True)]
        if '/exists/' in path:
            return 200, copy.deepcopy(self.exists)
        if path.endswith('/messages'):
            return 200, copy.deepcopy(self.pages[1 if query.get('cursor') else 0] if self.pages else self.page)
        raise AssertionError('unexpected provider read')
    def reconcile(self):
        return self.client.reconcile(PLAN, NOW)


class DmIdentityTests(unittest.TestCase):
    def test_explicit_account_owner_binding_and_distinct_recipient_aliases(self):
        for pin in (PUBLIC, OWNER, 'urn:li:fsd_profile:'+OWNER):
            with self.subTest(pin=pin):
                fixture=ReadFixture(pin)
                result=fixture.reconcile()
                self.assertEqual('sent',result['status'])
                self.assertEqual(RECEIPT,result['receipt_ref'])
                self.assertEqual('observed_outbound',result['confirmation_kind'])
                self.assertIs(result['write_attribution_confirmed'],False)
                self.assertTrue(all(call[0]=='GET' for call in fixture.calls))
                self.assertEqual(pin,fixture.client.self_profile_id)

    def test_new_owner_binding_requires_safe_exact_public_url_and_explicit_account(self):
        for url in ('https://www.linkedin.com/in/other', 'http://www.linkedin.com/in/'+PUBLIC,
                    'https://www.linkedin.com.evil/in/'+PUBLIC,
                    'https://user@www.linkedin.com/in/'+PUBLIC,
                    'https://www.linkedin.com:443/in/'+PUBLIC, None):
            with self.subTest(url=url):
                fixture=ReadFixture()
                fixture.page['messages'][0]['sender']['profileUrl']=url
                self.assertEqual('uncertain',fixture.reconcile()['status'])
        fixture=ReadFixture();fixture.page.pop('accountId')
        self.assertEqual('uncertain',fixture.reconcile()['status'])

    def test_conflicting_sender_owner_pin_namespace_and_outbound_flag_fail_closed(self):
        variants=[('senderId','OTHER'),('isSentByOwner',False),('isSentByOwner',None),
                  ('messageUrn','urn:li:msg_message:(urn:li:fsd_profile:OTHER,MESSAGE)')]
        for key,value in variants:
            fixture=ReadFixture();fixture.page['messages'][0][key]=value
            try: result=fixture.reconcile()
            except ProviderError: continue
            self.assertEqual('uncertain',result['status'])
        fixture=ReadFixture('PINNED_OTHER')
        self.assertEqual('uncertain',fixture.reconcile()['status'])
        fixture=ReadFixture();fixture.page['messages'].append({**MESSAGE,'senderProfileId':'OTHER',
            'sender':{'profileId':'OTHER','profileUrl':'https://www.linkedin.com/in/'+PUBLIC}})
        self.assertEqual('uncertain',fixture.reconcile()['status'])

    def test_duplicate_unknown_third_or_missing_participant_is_not_an_alias(self):
        variants=[[PUBLIC,'fixture-target','fixture-target'],[PUBLIC,'fixture-target','THIRD'],
                  [PUBLIC,'fixture-target','UNKNOWN'],['fixture-target','RECIPIENT'],
                  [PUBLIC,PUBLIC,'fixture-target']]
        for ids in variants:
            fixture=ReadFixture();fixture.page['participants']=[dict(profileId=i,name='Same name') for i in ids]
            self.assertEqual('uncertain',fixture.reconcile()['status'])

    def test_wrong_account_recipient_or_thread_rejected(self):
        for field,value in [('accountId','b'*24),('conversationUrn','urn:li:msg_conversation:OTHER')]:
            fixture=ReadFixture();fixture.page[field]=value
            with self.assertRaises(ProviderError):fixture.reconcile()
        fixture=ReadFixture();fixture.exists['profileUrn']='urn:li:fsd_profile:OTHER'
        with self.assertRaises(ProviderError):fixture.reconcile()

    def test_contradictory_cursor_page_retains_uncertainty_and_static_diagnosis(self):
        fixture=ReadFixture()
        first={**fixture.page,'hasMore':True,'nextCursor':str(NOW*1000)}
        second={**fixture.page,'messages':[],'hasMore':True,'nextCursor':None}
        fixture.pages=[first,second]
        result=fixture.reconcile()
        self.assertEqual({'status':'uncertain','error_code':'pagination_contradictory'},result)
        self.assertEqual(2,sum(c[1].endswith('/messages') for c in fixture.calls))
        self.assertEqual('pagination_contradictory',ConnectSafelyConsole._result(result)['error_code'])
        self.assertFalse(ConnectSafelyConsole._result(result)['retry_write'])

    def test_terminal_page_with_cursor_or_fetching_does_not_confirm(self):
        for field,value in [('nextCursor',str(NOW*1000)),('fetchingMore',True)]:
            fixture=ReadFixture();fixture.page[field]=value
            self.assertEqual('uncertain',fixture.reconcile()['status'])

    def test_has_more_without_cursor_never_confirms_even_with_exact_message(self):
        fixture=ReadFixture();fixture.page['hasMore']=True
        self.assertEqual({'status':'uncertain','error_code':'pagination_contradictory'},fixture.reconcile())

    def test_receipt_body_time_or_ambiguity_never_confirms(self):
        for key,value in [('text','Different content'),('sentAt',(NOW-1)*1000),('sentAt',(NOW+121)*1000)]:
            fixture=ReadFixture();fixture.page['messages'][0][key]=value
            self.assertEqual('uncertain',fixture.reconcile()['status'])
        fixture=ReadFixture();fixture.page['messages'].append({**MESSAGE,
            'messageUrn':'urn:li:msg_message:(urn:li:fsd_profile:OWNER,SECOND)'})
        self.assertEqual('uncertain',fixture.reconcile()['status'])

    def test_alias_binding_is_not_learned_by_send(self):
        fixture=ReadFixture()
        with self.assertRaisesRegex(ProviderError,'receipt_unverified'):
            fixture.client._receipt(MESSAGE,PLAN,NOW)
        self.assertEqual(PUBLIC,fixture.client.self_profile_id)

    def test_modern_thread_namespace_must_match_even_a_pinned_member(self):
        fixture=ReadFixture(OWNER)
        thread='urn:li:msg_conversation:(urn:li:fsd_profile:OTHER,THREAD)'
        fixture.exists['conversationUrn']=fixture.page['conversationUrn']=thread
        result=fixture.client.reconcile({**PLAN,'conversation_urn':None},NOW)
        self.assertEqual('uncertain',result['status'])

    def test_public_alias_cannot_conflict_with_the_other_pinned_member(self):
        member='owner-member';fixture=ReadFixture(member)
        thread='urn:li:msg_conversation:(urn:li:fsd_profile:'+member+',THREAD)'
        fixture.exists['conversationUrn']=fixture.page['conversationUrn']=thread
        fixture.page['participants']=[dict(profileId=PUBLIC),dict(profileId=member)]
        fixture.page['messages']=[{**MESSAGE,'messageUrn':'urn:li:msg_message:(urn:li:fsd_profile:'+member+',MESSAGE)',
            'senderProfileId':member,'sender':{'profileId':member,'profileUrl':'https://www.linkedin.com/in/'+PUBLIC}}]
        result=fixture.client.reconcile({**PLAN,'conversation_urn':None,'recipient_public_id':member},NOW)
        self.assertEqual('uncertain',result['status'])


class DmJobTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.path=self.root/'canonical.db'
        @contextmanager
        def database():
            con=sqlite3.connect(self.path)
            try:yield con,'?';con.commit()
            except Exception:con.rollback();raise
            finally:con.close()
        self.database=database
        with database() as (con,_):
            con.execute('CREATE TABLE toques (id INTEGER PRIMARY KEY,canal TEXT,acao TEXT,pessoa_chave TEXT,alvo TEXT,status TEXT,provider_id TEXT,ref TEXT,agente TEXT,detalhe TEXT,atualizado_em TEXT)')
            con.execute('CREATE TABLE thunder_effect_touches (effect_key TEXT PRIMARY KEY,touch_id INTEGER)')
            con.execute('CREATE TABLE thunder_suppressions (canal TEXT,pessoa_chave TEXT,reason TEXT)')
            con.execute('CREATE TABLE respostas (id INTEGER PRIMARY KEY,canal TEXT,pessoa_chave TEXT)')
        workflow = self.root/'workflow.db'
        workflow.touch()
        env = patch.dict(os.environ, {'THUNDER_APOLLO_LI_OFFICIAL_DB': str(workflow.resolve())})
        env.start(); self.addCleanup(env.stop)
        self.repo=Repository(workflow);self.fixture=ReadFixture()
        self.ledger=ConnectSafelyLedger(db_context=database)
        self.console=ConnectSafelyConsole(self.repo,self.fixture.client,self.ledger,
            account_id=ACCOUNT,account_public_id=PUBLIC,writes_enabled=True,clock=lambda:NOW)
        self.app=ConnectSafelyApplication(self.repo,direct_console=self.console,
            execution_enabled=True,readiness=lambda:{'ready':True},execution_scope='fixture-only',clock=lambda:NOW)
        store, sheet=make_sheet(self.root/'sheet-brain')
        self.console.campaign_sheet_store=store
        CampaignRegistry(self.repo).register({
            'campaign_id':'fixture-dm', 'campaign_version':'1', 'rules_version':'1',
            'run_id':'fixture-run', 'platform':'thunder_os',
            'providers':{'linkedin':'connectsafely'}, 'rules':'Synthetic fixtures only',
            'briefing':sheet, 'configuration':{'flow_version':'1',
            'provider_accounts':{'linkedin':ACCOUNT}}}, 'ygor', 0)
        preview=self.app.prepare(PLAN,'ygor')
        self.preview_id=preview['id'];self.body={'preview_id':self.preview_id}
        review=dict(preview_id=self.preview_id,expected_version=preview['version'],review_hash=preview['review_hash'],
                    execution_confirmation=self.app.campaign_review(
                        {'preview_id':self.preview_id,'campaign_id':'fixture-dm'},'ygor'))
        job=self.app.enqueue(review,'ygor');self.job_id=job['job_id']
        def original_timeout(plan,before_dispatch,execution_context=None):
            self.assertTrue(before_dispatch())
            return {'status':'uncertain','error_code':'provider_outcome_unknown'}
        with patch.object(self.console,'_send',side_effect=original_timeout):
            self.assertTrue(self.app.process_one(job_id=self.job_id))
        self.effect=payload_hash(PLAN);self.fixture.calls.clear()
        self.assertEqual('needs_reconciliation',self.job()['state'])
    def job(self):return self.repo.get_state('connectsafely_job',self.job_id)
    def attempt(self):return self.repo.get_state('connectsafely_attempt',self.effect)
    def canonical(self):
        with self.database() as (con,_):return con.execute('SELECT status,provider_id FROM toques').fetchone()
    def change(self,kind,key,**fields):
        value=self.repo.get_state(kind,key);return self.repo.put_state(kind,key,{**value,**fields})
    def reconcile(self):return self.app.reconcile(self.body,'ygor')

    def test_exact_observation_finishes_owned_touch_attempt_job_and_one_audit(self):
        first=self.reconcile();reads=len(self.fixture.calls)
        self.assertEqual('sent',first['status']);self.assertFalse(first['write_attribution_confirmed'])
        self.assertFalse(first['retry_write']);self.assertEqual(('feito',RECEIPT),self.canonical())
        self.assertEqual('sent',self.attempt()['status']);self.assertEqual('done',self.job()['state'])
        self.assertEqual(1,len(self.repo.list_state('connectsafely_reconciliation')))
        self.assertEqual(first,self.reconcile());self.assertEqual(reads,len(self.fixture.calls))
        self.assertEqual(1,len(self.repo.list_state('connectsafely_reconciliation')))

    def test_delegated_reconciliation_checks_authority_and_updates_job_without_send(self):
        authority={'fixture':'delegation'}
        self.change('connectsafely_job',self.job_id,actor='hermes (delegado)',delegated_authority=authority)
        self.change('connectsafely_preview_origin',self.preview_id,
                    executor='hermes (delegado)',delegated_authority=authority)
        with patch.object(self.app,'_delegated_reconciliation_authorized',return_value=None) as guard, \
             patch.object(self.console,'_send',side_effect=AssertionError('no dispatch')):
            result=self.app.reconcile_delegated(self.body,authority)
            self.assertEqual('sent',result['status'])
            self.assertEqual('done',self.job()['state'])
            self.assertFalse(result['retry_write'])
            self.assertGreaterEqual(guard.call_count,2)
            self.assertEqual(result,self.app.reconcile_delegated(self.body,authority))
        self.assertEqual(1,len(self.repo.list_state('connectsafely_reconciliation')))

    def test_delegated_reconciliation_denied_authority_never_reads_or_updates(self):
        authority={'fixture':'delegation'}
        self.change('connectsafely_job',self.job_id,actor='hermes (delegado)',delegated_authority=authority)
        self.change('connectsafely_preview_origin',self.preview_id,
                    executor='hermes (delegado)',delegated_authority=authority)
        with patch.object(self.app,'_delegated_reconciliation_authorized',
                          side_effect=ConnectSafelyError('job_authority_invalid')):
            with self.assertRaises(ConnectSafelyError):
                self.app.reconcile_delegated(self.body,authority)
        self.assertEqual([],self.fixture.calls)
        self.assertEqual(('incerto',None),self.canonical())
        self.assertEqual('needs_reconciliation',self.job()['state'])

    def test_original_approval_expiry_does_not_require_new_dispatch_authority(self):
        self.app.clock=self.console.clock=lambda:NOW+1000
        self.assertEqual('sent',self.reconcile()['status'])
        self.assertFalse(self.job()['result']['write_attribution_confirmed'])

    def test_incomplete_history_preserves_touch_and_job_and_no_retry(self):
        self.fixture.pages=[{**self.fixture.page,'hasMore':True,'nextCursor':str(NOW*1000)},
                            {**self.fixture.page,'messages':[],'hasMore':True,'nextCursor':None}]
        result=self.reconcile()
        self.assertEqual('uncertain',result['status']);self.assertEqual('pagination_contradictory',result['error_code'])
        self.assertFalse(result['retry_write']);self.assertEqual(('incerto',None),self.canonical())
        self.assertEqual('needs_reconciliation',self.job()['state'])
        self.assertEqual([],self.repo.list_state('connectsafely_reconciliation'))

    def test_invalid_original_authority_or_token_rejected_before_reads(self):
        cases=[('connectsafely_job',self.job_id,{'actor':'other'}),
               ('connectsafely_job',self.job_id,{'execution_scope':'other'}),
               ('connectsafely_job',self.job_id,{'result':{'attempt_token':'other'}}),
               ('connectsafely_queue_approval',self.job_id,{'approved_at':NOW+1}),
               ('connectsafely_queue_approval',self.job_id,{'expires_at':NOW}),
               ('connectsafely_attempt',self.effect,{'approval_hash':'other'}),
               ('connectsafely_attempt',self.effect,{'attempt_token':'other'})]
        for kind,key,fields in cases:
            original=self.repo.get_state(kind,key)
            with self.subTest(kind=kind,fields=fields):
                self.change(kind,key,**fields)
                with self.assertRaises((ConnectSafelyError,RepositoryConflict)):self.reconcile()
                self.assertEqual([],self.fixture.calls);self.assertEqual(('incerto',None),self.canonical())
                self.repo.put_state(kind,key,original)

    def test_duplicate_job_is_not_authority(self):
        self.repo.append_state('connectsafely_job','foreign-job',self.job())
        with self.assertRaisesRegex(ConnectSafelyError,'job_authority_invalid'):self.reconcile()
        self.assertEqual([],self.fixture.calls)

    def test_duplicate_job_added_during_provider_read_blocks_commit(self):
        original=self.fixture.client.request
        def raced(method,path,query,body):
            response=original(method,path,query,body)
            if path.endswith('/messages'):
                self.repo.append_state('connectsafely_job','foreign-job',self.job())
            return response
        with patch.object(self.fixture.client,'request',side_effect=raced):
            with self.assertRaises(RepositoryConflict):self.reconcile()
        self.assertEqual(('incerto',None),self.canonical())

    def test_done_job_with_conflicting_status_or_retry_cannot_replay_as_confirmed(self):
        self.reconcile();original=self.job()
        for fields in ({'status':'uncertain'},{'retry_write':True}):
            self.change('connectsafely_job',self.job_id,result={**original['result'],**fields})
            with self.assertRaises(RepositoryConflict):self.reconcile()
            self.repo.put_state('connectsafely_job',self.job_id,original)

    def test_attempt_preview_job_or_approval_race_blocks_canonical_commit(self):
        cases=[('connectsafely_attempt',self.effect,{'attempt_token':'other'}),
               ('connectsafely_preview',self.preview_id,{'attempt_token':'other'}),
               ('connectsafely_job',self.job_id,{'actor':'other'}),
               ('connectsafely_queue_approval',self.job_id,{'actor':'other'})]
        original_request=self.fixture.client.request
        for kind,key,fields in cases:
            original=self.repo.get_state(kind,key);changed=False
            def raced(method,path,query,body):
                nonlocal changed
                response=original_request(method,path,query,body)
                if path.endswith('/messages') and not changed:
                    changed=True;self.change(kind,key,**fields)
                return response
            with self.subTest(kind=kind),patch.object(self.fixture.client,'request',side_effect=raced):
                with self.assertRaises(RepositoryConflict):self.reconcile()
                self.assertEqual(('incerto',None),self.canonical())
                self.assertEqual([],self.repo.list_state('connectsafely_reconciliation'))
            self.repo.put_state(kind,key,original)

    def test_foreign_mapping_or_terminal_receipt_never_finishes_job(self):
        with self.database() as (con,_):con.execute("UPDATE toques SET ref='foreign'")
        with self.assertRaises(TouchBlocked):self.reconcile()
        self.assertEqual(('incerto',None),self.canonical());self.assertNotEqual('done',self.job()['state'])
        with self.database() as (con,_):con.execute("UPDATE toques SET ref=?,status='feito',provider_id='connectsafely:urn:li:messagingMessage:OTHER'",(self.preview_id,))
        with self.assertRaises(TouchBlocked):self.reconcile()
        self.assertNotEqual('done',self.job()['state'])

    def test_crash_after_canonical_commit_recovers_saved_receipt_without_provider_reads(self):
        original=self.ledger.reconcile_dm
        def commit_then_crash(*args):
            original(*args);raise RuntimeError('synthetic crash after canonical commit')
        with patch.object(self.ledger,'reconcile_dm',side_effect=commit_then_crash):
            with self.assertRaises(RuntimeError):self.reconcile()
        self.assertEqual(('feito',RECEIPT),self.canonical())
        self.assertEqual('uncertain',self.attempt()['status']);self.assertEqual('needs_reconciliation',self.job()['state'])
        with patch.object(self.fixture.client,'request',side_effect=AssertionError('receipt must be replayed locally')):
            self.assertEqual('sent',self.reconcile()['status'])
        self.assertEqual(1,len(self.repo.list_state('connectsafely_reconciliation')))

    def test_attempt_store_failure_preserves_observation_for_exact_local_recovery(self):
        with patch.object(self.console,'_store',side_effect=RuntimeError('synthetic journal failure')):
            with self.assertRaises(RuntimeError):self.reconcile()
        self.assertEqual(('feito',RECEIPT),self.canonical());self.assertEqual('uncertain',self.attempt()['status'])
        with patch.object(self.fixture.client,'request',side_effect=AssertionError('no repeated reads')):
            self.assertEqual('sent',self.reconcile()['status'])

    def test_saved_receipt_cannot_be_rebound_to_changed_attempt_time_or_touch(self):
        with patch.object(self.console,'_store',side_effect=RuntimeError('synthetic journal failure')):
            with self.assertRaises(RuntimeError):self.reconcile()
        original=self.attempt()
        for fields in ({'attempted_at':NOW+1},{'canonical_touch_id':'999'},{'canonical_ref':'foreign'}):
            with self.subTest(fields=fields):
                self.change('connectsafely_attempt',self.effect,**fields)
                with patch.object(self.fixture.client,'request',side_effect=AssertionError('no repeated reads')):
                    with self.assertRaises(RepositoryConflict):self.reconcile()
                self.assertEqual('needs_reconciliation',self.job()['state'])
                self.repo.put_state('connectsafely_attempt',self.effect,original)

    def test_job_audit_failure_is_atomic_and_resumes_from_exact_terminal_attempt(self):
        original=self.repo.append_state
        def fail_audit(kind,*args,**kwargs):
            if kind=='connectsafely_reconciliation':raise RuntimeError('synthetic audit failure')
            return original(kind,*args,**kwargs)
        with patch.object(self.repo,'append_state',side_effect=fail_audit):
            with self.assertRaises(RuntimeError):self.reconcile()
        self.assertEqual('sent',self.attempt()['status']);self.assertEqual('needs_reconciliation',self.job()['state'])
        self.assertEqual([],self.repo.list_state('connectsafely_reconciliation'))
        with patch.object(self.fixture.client,'request',side_effect=AssertionError('no repeated reads')):
            self.assertEqual('sent',self.reconcile()['status'])
        self.assertEqual(1,len(self.repo.list_state('connectsafely_reconciliation')))

    def test_sent_attempt_fast_path_still_validates_scoped_observation(self):
        with patch.object(self.repo,'append_state',side_effect=RuntimeError('synthetic audit failure')):
            with self.assertRaises(RuntimeError):self.reconcile()
        original=self.attempt()
        for fields in ({'attempted_at':NOW+130},{'canonical_touch_id':'999'},{'canonical_ref':'foreign'},
                       {'reconciliation_observation':{**original['reconciliation_observation'],'attempt_token':'other'}}):
            with self.subTest(fields=fields):
                self.change('connectsafely_attempt',self.effect,**fields)
                with self.assertRaises(RepositoryConflict):self.reconcile()
                self.assertEqual('needs_reconciliation',self.job()['state'])
                self.repo.put_state('connectsafely_attempt',self.effect,original)

    def test_done_job_replay_rejects_changed_observation_hash_or_receipt(self):
        self.reconcile();original=self.attempt();observation=original['reconciliation_observation']
        for fields in ({'reconciliation_observation':{**observation,'result_hash':'other'}},
                       {'receipt_ref':'connectsafely:urn:li:messagingMessage:OTHER'},
                       {'reconciliation_observation':None}):
            self.change('connectsafely_attempt',self.effect,**fields)
            with self.assertRaises((ConnectSafelyError,TouchBlocked,RepositoryConflict)):self.reconcile()
            self.repo.put_state('connectsafely_attempt',self.effect,original)

    def test_terminal_attempt_raw_attribution_and_retry_flags_cannot_be_normalized_away(self):
        with patch.object(self.repo,'append_state',side_effect=RuntimeError('synthetic audit failure')):
            with self.assertRaises(RuntimeError):self.reconcile()
        for state in ('needs_reconciliation','done'):
            if state=='done':self.reconcile()
            original=self.attempt()
            for fields in ({'write_attribution_confirmed':True},{'retry_write':True},
                           {'write_attribution_confirmed':0},{'retry_write':0}):
                with self.subTest(state=state,fields=fields):
                    self.change('connectsafely_attempt',self.effect,**fields)
                    with self.assertRaises((ConnectSafelyError,RepositoryConflict)):self.reconcile()
                    self.assertFalse(self.job()['result']['retry_write'])
                    self.repo.put_state('connectsafely_attempt',self.effect,original)

    def test_original_provider_receipt_path_remains_supported_without_observation(self):
        plan={**PLAN,'content':'Different original synthetic dispatch'}
        preview=self.app.prepare(plan,'ygor')
        job=self.app.enqueue(dict(preview_id=preview['id'],expected_version=preview['version'],review_hash=preview['review_hash'], execution_confirmation=self.app.campaign_review({'preview_id':preview['id'],'campaign_id':'fixture-dm'},'ygor')),'ygor')
        def original_receipt(plan,before_dispatch,execution_context=None):
            self.assertTrue(before_dispatch())
            return {'status':'sent','receipt_ref':RECEIPT,'confirmation_kind':'provider_receipt','write_attribution_confirmed':True}
        with patch.object(self.console,'_send',side_effect=original_receipt):
            self.assertTrue(self.app.process_one(job_id=job['job_id']))
        attempt=self.repo.get_state('connectsafely_attempt',payload_hash(plan))
        self.assertNotIn('reconciliation_observation',attempt)
        with patch.object(self.fixture.client,'request',side_effect=AssertionError('no repeated reads')):
            result=self.app.reconcile({'preview_id':preview['id']},'ygor')
        self.assertEqual('sent',result['status']);self.assertTrue(result['write_attribution_confirmed'])
        self.assertEqual('provider_receipt',result['confirmation_kind'])

    def test_already_sent_attempt_requires_exact_ledger_readback_before_job_done(self):
        with patch.object(self.repo,'append_state',side_effect=RuntimeError('synthetic audit failure')):
            with self.assertRaises(RuntimeError):self.reconcile()
        with self.database() as (con,_):con.execute("UPDATE toques SET provider_id='connectsafely:urn:li:messagingMessage:OTHER'")
        with self.assertRaises(TouchBlocked):self.reconcile()
        self.assertEqual('needs_reconciliation',self.job()['state'])

    def test_job_failure_rolls_back_audit_and_replay_keeps_single_audit(self):
        original=self.repo.put_state
        def fail_job(kind,*args,**kwargs):
            if kind=='connectsafely_job':raise RuntimeError('synthetic job failure')
            return original(kind,*args,**kwargs)
        with patch.object(self.repo,'put_state',side_effect=fail_job):
            with self.assertRaises(RuntimeError):self.reconcile()
        self.assertEqual([],self.repo.list_state('connectsafely_reconciliation'))
        with patch.object(self.fixture.client,'request',side_effect=AssertionError('no repeated reads')):
            self.assertEqual('sent',self.reconcile()['status'])
        self.assertEqual(1,len(self.repo.list_state('connectsafely_reconciliation')))

    def test_ledger_enforces_touch_ref_person_action_target_mapping_status_and_receipt(self):
        step=self.console._canonical_step(self.console._step(PLAN,self.preview_id,1),PLAN)
        attempt=self.attempt();args=(self.effect,attempt['canonical_touch_id'],self.preview_id,step,RECEIPT)
        variants=[('other',*args[1:]),(args[0],'999',*args[2:]),(args[0],args[1],'foreign',*args[3:]),
                  (*args[:3],{**step,'recipient':'https://www.linkedin.com/in/other'},RECEIPT),
                  (*args[:3],{**step,'content':'different'},RECEIPT),
                  (*args[:3],{**step,'action':'invite'},RECEIPT),(*args[:4],'fake')]
        for variant in variants:
            with self.assertRaises(TouchBlocked):self.ledger.reconcile_dm(*variant)
        self.assertEqual(('incerto',None),self.canonical())
        self.ledger.reconcile_dm(*args)
        self.assertEqual(self.ledger.reconcile_dm(*args),self.ledger.read_reconciled_dm(*args))
        with self.assertRaises(TouchBlocked):self.ledger.reconcile_dm(*args[:4],'connectsafely:urn:li:messagingMessage:OTHER')
        with self.database() as (con,_):con.execute("UPDATE toques SET status='liberado'")
        with self.assertRaises(TouchBlocked):self.ledger.reconcile_dm(*args)


if __name__=='__main__':unittest.main()
