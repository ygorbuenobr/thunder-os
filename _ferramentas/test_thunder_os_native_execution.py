"""S51 named proofs: real local workflow/ledgers, fake provider ports, no network."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.approvals import _step_hash
from thunder_os.db import Repository
from thunder_os.models import payload_hash
from thunder_os.service import ThunderService
from thunder_os.touch_ledger import TouchLedger
from thunder_os.connectsafely_ledger import ConnectSafelyLedger
from thunder_os.connectsafely_console import ConnectSafelyConsole
from thunder_os.connectsafely_application import ConnectSafelyApplication
from thunder_os.native_cadence import ACTION_FIELDS
from thunder_os.native_cadence_execution import NativeCadenceExecutor, NativeRoute
from thunder_os.thunder_email_runtime import GmailCadenceAdapter
from thunder_os.seqd_client import MAILBOX
from thunder_os.worker import WorkerRuntime
from thunder_os.providers.result import ProviderResult
from thunder_os.reply_control import ReplyControl
from test_campaign_sheet_support import make_sheet
from test_thunder_os_research import base_artifact

CATALOG = {'version':'s51-offline','status':'validado','capabilities':['cap-1'],'proofs':['proof-1']}


class Crash(BaseException):
    pass


class FakeCS:
    def __init__(self, account, clock):
        self.account, self.clock = account, clock
        self.sends, self.reads = [], []
        self.mode = 'confirmed'
        self.observation = None
        self.hook = None

    def verify_identity(self):
        return {'verified':True,'account_id':self.account,'account_public_id':self.account+'-public'}

    def send(self, plan, *, before_dispatch):
        if self.hook:
            self.hook()
        assert before_dispatch() is True
        self.sends.append(deepcopy(plan))
        if self.mode == 'crash':
            raise Crash('fake process died after provider effect')
        if self.mode == 'timeout':
            raise TimeoutError('fake lost receipt')
        return self.result()

    def result(self,plan=None):
        index=self.sends.index(plan)+1 if plan in self.sends else len(self.sends)
        return {'status':'sent','receipt_ref':('connectsafely:urn:li:invitation:fixture' if (plan or self.sends[-1])['action']=='invite'
                else 'connectsafely:urn:li:msg:fixture')+str(index), 'confirmation_kind':'provider_receipt','write_attribution_confirmed':True}

    def reconcile(self, plan, attempted_at):
        self.reads.append(deepcopy(plan))
        if not self.sends or self.mode in ('timeout','crash','mismatch'):
            return {'status':'uncertain','error_code':'receipt_unverified'}
        return self.result(plan)

    def relationship(self, plan):
        return deepcopy(self.observation) if self.observation is not None else {
            'account_id':self.account,'recipient_public_id':plan['recipient_public_id'],
            'recipient_profile_urn':plan['recipient_profile_urn'],'relationship_status':'CONNECTED','observed_at':self.clock()}


class FakeGmail:
    def __init__(self, clock):
        self.clock=clock;self.sends=[];self.messages={};self.mode='confirmed';self.exact=True
    def status(self):
        return {'mailbox':MAILBOX,'status':'ready','capabilities':{'exact_reply_parent':self.exact}}
    def send(self, envelope):
        self.sends.append(deepcopy(envelope));ident=format(len(self.sends),'x')+'abc'
        parent=self.messages.get(envelope.get('reply_message_id'))
        mid='<s51-'+ident+'@example.invalid>'
        headers=[{'name':'Message-ID','value':mid}]
        if parent:
            ref=parent['message_id'];prior=next((h['value'] for h in parent['headers'] if h['name']=='References'),'')
            refs=prior+' '+ref if prior else ref
            headers += [{'name':'In-Reply-To','value':ref},{'name':'References','value':refs}]
        self.messages[ident]={'id':ident,'thread_id':envelope.get('thread_id') or 'def123',
            'from':MAILBOX,'to':envelope['to'],'subject':envelope['subject'],'body':envelope['body'],
            'date':format_datetime(datetime.fromtimestamp(self.clock(),timezone.utc)),
            'sent':True,'cc':'','bcc':'','truncated':False,'message_id':mid,'headers':headers,'headers_complete':True}
        if self.mode=='crash':raise Crash('fake died after Gmail effect')
        if self.mode=='timeout':raise TimeoutError('fake lost Gmail result')
        return {'mailbox':MAILBOX,'status':'sent','provider_id':ident}
    def read(self, ident):
        message=deepcopy(self.messages[ident])
        if self.mode=='mismatch':message['to']='other@example.invalid'
        return {'mailbox':MAILBOX,'status':'ok','message':message}
    def reconcile(self,key):
        if self.mode in ('crash','timeout'):return {'mailbox':MAILBOX,'status':'uncertain'}
        return {'mailbox':MAILBOX,'status':'sent','provider_id':list(self.messages)[-1]}


class NativeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='s51-native-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=datetime(2026,10,27,15,tzinfo=timezone.utc)
        outer=self
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):
                return outer.now.astimezone(tz) if tz else outer.now.replace(tzinfo=None)
        for module in ('worker','service','db','approvals','reply_control'):
            guard=patch('thunder_os.'+module+'.datetime',Frozen);guard.start();self.addCleanup(guard.stop)
        for name in ('socket.socket','socket.create_connection'):
            guard=patch(name,side_effect=AssertionError('S51 network forbidden'));guard.start();self.addCleanup(guard.stop)
        guard=patch('thunder_os.touch_ledger.toques._banco',side_effect=AssertionError('production DB forbidden'))
        guard.start();self.addCleanup(guard.stop)
        self.repo=Repository(self.root/'workflow.db');self.touchpath=self.root/'touches.db'
        with self.database() as (con,_):
            con.executescript('''CREATE TABLE toques(id INTEGER PRIMARY KEY,canal TEXT,acao TEXT,pessoa_chave TEXT,alvo TEXT,status TEXT,provider_id TEXT,detalhe TEXT,agente TEXT,ref TEXT,atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE respostas(id INTEGER PRIMARY KEY,canal TEXT,pessoa_chave TEXT,pessoa_nome TEXT,conta TEXT,toque_id INTEGER,classificacao TEXT,resumo TEXT,ref TEXT);
            CREATE TABLE thunder_suppressions(canal TEXT,pessoa_chave TEXT,reason TEXT,PRIMARY KEY(canal,pessoa_chave));
            CREATE TABLE thunder_effect_touches(effect_key TEXT PRIMARY KEY,touch_id INTEGER);''')
        self.ledger=TouchLedger(db_context=self.database);self.csledger=ConnectSafelyLedger(db_context=self.database)
        self.mail=FakeGmail(self.clock);self.cs={};self.apps={};self.packages={};self.bindings={};self.dossier=base_artifact()
        self.readers={'source':lambda ref:self.packages[ref['path']], 'research':lambda ref,selection:deepcopy(self.dossier),
                      'crm':lambda selection:deepcopy(self.bindings[selection[0].split('-')[0]])}
        self.service=ThunderService(self.repo,self.ledger,approved_catalog=CATALOG,native_readers=self.readers,native_clock=lambda:self.now)
        self.sheet,briefing=make_sheet(self.root/'brain');self.service.campaign_sheet_store=self.sheet
        self.config={'platform':'thunder_os','campaign_id':'s51-offline','campaign_version':'1','rules_version':'1',
            'run_id':'s51-fixture','rules':'Synthetic only','providers':{'email':'gmail','linkedin':'connectsafely'},
            'configuration':{'flow_version':'mexico_native_v1','provider_accounts':{'email':MAILBOX,'linkedin':'ygor-account'}},'briefing':briefing}
        self.context=self.service.configure_campaign(self.config,'ygor',expected_version=0)['context']
        self.cadences={}

    def clock(self):return self.now.timestamp()

    @contextmanager
    def database(self):
        con=sqlite3.connect(self.touchpath,timeout=1)
        try:
            with con:yield con,'?'
        finally:con.close()

    def setup_route(self,route, *, full=False, contact='100', campaign=None, invite_note='', source_campaign='s51'):
        # Registry fixture is explicitly created only in the temporary workflow DB.
        config=deepcopy(self.config);config['campaign_id']=campaign or 's51-'+route
        config['configuration']['provider_accounts']['linkedin']=route+'-account'
        context=self.service.configure_campaign(config,'ygor',expected_version=0)['context']
        self.cs[route]=FakeCS(route+'-account',self.clock)
        console=ConnectSafelyConsole(self.repo,self.cs[route],self.csledger,account_id=route+'-account',
            account_public_id=route+'-account-public',writes_enabled=True,clock=self.clock)
        self.apps[route]=ConnectSafelyApplication(self.repo,direct_console=console,provider=self.cs[route],
            execution_enabled=True,readiness=lambda:{'ready':True},clock=self.clock)
        plan=[('E1','EMAIL'),('L1','CONNECTION_REQUEST'),('E2','EMAIL'),('L2','MESSAGE'),('E3','EMAIL')] if route=='ygor' else [('L1','CONNECTION_REQUEST'),('L2','MESSAGE'),('L3','MESSAGE'),('L4','MESSAGE')]
        if not full:plan=plan[:2] if route=='ygor' else plan[:1]
        actions=[]
        for label,kind in plan:
            email=kind=='EMAIL';key=route+'-'+label
            actions.append({'key':key,'step':label,'channel':'email' if email else 'linkedin','action':kind,'sender':route,
                'account_id':MAILBOX if email else route+'-account','recipient':'person@example.invalid' if email else 'https://www.linkedin.com/in/s51-person',
                'subject':'Exact synthetic subject' if email else '','text':invite_note if kind=='CONNECTION_REQUEST' else '  Literal '+label+'\n',
                'contact_id':contact,'task_id':str(1000+len(actions)),'editorial_status':'PREPARED_FOR_EXACT_REVIEW','person_id':'fixture-person','campaign':source_campaign} )
        package={'actions':actions,'people':[{'id':'fixture-person','route':route,'holds':[]}],
            'approval_payload':{'actions':[{key:a[key] for key in ACTION_FIELDS} for a in actions]}}
        package['approval']={'package_sha256':payload_hash(package['approval_payload'])}
        raw=json.dumps(package).encode();path=route+'.json';self.packages[path]=raw
        records=[]
        for dest in set(a['recipient'] for a in actions):
            records.extend([{'domain':'acme.example','person_key':dest,'source':'fixture:'+str(i),'hubspot_contact_id':contact} for i in range(2)])
        self.bindings[route]={'portal_id':'999','company_id':'200','contact_id':contact,'deal_id':'300',
            'task_ids':{a['key']:a['task_id'] for a in actions},'identity_records':records}
        prepared=self.service.prepare_native_cadence({'path':path,'sha256':hashlib.sha256(raw).hexdigest()},[a['key'] for a in actions],
            route=route,target_context=context,expected_version=0,actor='ygor')
        cid=prepared['cadence_id'];self.assertIsNotNone(cid)
        cadence=self.repo.get_state('cadence',cid)
        self.service.approve_cadence(cid,'ygor',expected_version=cadence['version'],review_hash=self.service.cadence_review_hash(cadence),
            execution_confirmation=self.service.campaign_execution_review(cadence))
        self.cadences[route]=cid
        return cid

    def executor(self):
        routes={}
        for route in self.cs:
            def plan(job,context,route=route):
                step=job['payload']['step']
                return {'account_id':route+'-account','account_public_id':route+'-account-public','action':step['native']['action'],
                    'recipient_public_id':'s51-person','recipient_profile_urn':'urn:li:fsd_profile:fixture',
                    'channel':'linkedin','messaging_channel':'linkedin_inbox','content':step['content'],'conversation_urn':None}
            routes[route]=NativeRoute(connectsafely_app=self.apps[route],plan_loader=plan,
                gmail_adapter=GmailCadenceAdapter(self.repo,client=self.mail,enabled=True,clock=self.clock),
                relationship_reader=self.cs[route].relationship)
        return NativeCadenceExecutor(self.repo,self.ledger,routes=routes,native_readers=self.readers,clock=self.clock,native_calendar=self.service._native_calendar)

    def worker(self,executor=True):
        worker=WorkerRuntime(self.repo,self.ledger,{},worker_id='s51-worker',health_path=self.root/'health.json',revision='s51-offline',
            approved_catalog=CATALOG,native_executor=self.executor() if executor else None)
        worker.authorization_service.campaign_sheet_store=self.sheet
        worker.authorization_service._native_clock=lambda:self.now
        return worker

    def dispatch(self,route,label):
        return self.service.dispatch_step(self.cadences[route],route+'-'+label,'ygor')

    def run_job(self,route,label):
        job=self.dispatch(route,label);self.assertTrue(self.worker().process_one());return self.repo.effect(job['id'])


    def test_cross_route_destinations_share_logical_person_day(self):
        self.setup_route('ygor',contact='100')
        self.setup_route('loa',contact='100')
        self.assertEqual(self.run_job('ygor','E1')['state'],'sent')
        with self.assertRaisesRegex(ValueError,'due|calendar'):
            self.dispatch('loa','L1')
        self.assertEqual(self.cs['loa'].sends,[])

    def test_cross_route_independent_source_campaign_is_not_occupied(self):
        self.setup_route('ygor',contact='100')
        self.setup_route('loa',contact='100',source_campaign='other-logical')
        self.assertEqual(self.run_job('ygor','E1')['state'],'sent')
        self.assertEqual(self.run_job('loa','L1')['state'],'sent')

    def test_cross_route_uncertain_or_reconciliation_retains_other_route(self):
        self.setup_route('ygor',contact='100');self.setup_route('loa',contact='100')
        self.mail.mode='timeout';job=self.run_job('ygor','E1')
        self.assertEqual(job['state'],'uncertain')
        for state in ('uncertain','needs_reconciliation'):
            with self.subTest(state=state):
                with self.repo.transaction() as con:
                    con.execute('UPDATE thunder_effect_jobs SET state=? WHERE id=?',(state,job['id']))
                with self.assertRaisesRegex(ValueError,'uncertain'):
                    self.dispatch('loa','L1')
        self.assertEqual(self.cs['loa'].sends,[])

    def test_calendar_pre_attempt_defers_and_resumes_next_window_once(self):
        self.now=datetime(2026,10,27,23,59,tzinfo=timezone.utc)
        self.setup_route('loa');job=self.dispatch('loa','L1')
        self.now+=timedelta(minutes=1)
        self.assertTrue(self.worker().process_one())
        self.assertEqual(self.repo.effect(job['id'])['state'],'pending')
        self.assertEqual(self.repo.list_state('connectsafely_attempt'),[])
        self.assertEqual(self.cs['loa'].sends,[])
        self.now=datetime(2026,10,28,15,tzinfo=timezone.utc)
        self.assertTrue(self.worker().process_one())
        self.assertEqual(self.repo.effect(job['id'])['state'],'sent')
        self.assertEqual(len(self.cs['loa'].sends),1)
        self.assertFalse(self.worker().process_one())

    def test_calendar_deferral_does_not_reopen_pause_revocation_or_expiry(self):
        for kind in ('pause','revoke','expire'):
            with self.subTest(kind=kind):
                case=NativeExecutionTests();case.setUp()
                try:
                    case.now=datetime(2026,10,27,23,59,tzinfo=timezone.utc)
                    case.setup_route('loa');job=case.dispatch('loa','L1')
                    case.now+=timedelta(minutes=1);case.worker().process_one()
                    self.assertEqual(case.repo.effect(job['id'])['state'],'pending')
                    case.now=datetime(2026,10,28,15,tzinfo=timezone.utc)
                    if kind=='pause':case.service.set_global_pause(True,'ygor')
                    elif kind=='revoke':
                        approval=case.repo.get_state('approval',case.cadences['loa'])
                        case.repo.put_state('approval',case.cadences['loa'],{**approval,'status':'revoked'})
                    else:case.now=datetime(2026,10,31,15,tzinfo=timezone.utc)
                    case.worker().process_one()
                    self.assertNotEqual(case.repo.effect(job['id'])['state'],'sent')
                    self.assertEqual(case.cs['loa'].sends,[])
                finally:case.doCleanups()

    def test_calendar_foreign_canonical_reservation_is_never_deferred(self):
        for route in ('ygor','loa'):
            with self.subTest(route=route):
                case=NativeExecutionTests();case.setUp()
                try:
                    case.now=datetime(2026,10,27,23,59,tzinfo=timezone.utc)
                    case.setup_route(route);label='E1' if route=='ygor' else 'L1'
                    job=case.dispatch(route,label);executor=case.executor();step=job['payload']['step']
                    if route=='ygor':
                        created,_=case.ledger.reserve('a'*64,step['id'],executor._email_touch_step(job,step))
                    else:
                        context=case.repo.get_state('cadence',case.cadences[route])['native_context']
                        plan=executor.routes[route].plan_loader(job,context);console=case.apps[route].direct
                        canonical=console._canonical_step(console._step(plan,'independent-preview',1),plan)
                        created,_=case.csledger.reserve('b'*64,'independent-preview',canonical)
                    self.assertTrue(created)
                    self.assertFalse(executor.preparation_absent(job))
                    case.now+=timedelta(minutes=1);case.worker().process_one()
                    self.assertEqual(case.repo.effect(job['id'])['state'],'needs_reconciliation')
                    self.assertEqual(case.mail.sends,[]);self.assertEqual(case.cs[route].sends,[])
                finally:case.doCleanups()

    def test_calendar_wait_with_durable_prior_reservation_is_not_requeued(self):
        self.now=datetime(2026,10,27,23,59,tzinfo=timezone.utc)
        self.setup_route('loa');job=self.dispatch('loa','L1');executor=self.executor()
        # Prepare alone persists a preview; imitate the independently committed
        # canonical reservation surviving loss of its workflow attempt marker.
        executor.prepare_job(job)
        plan=executor.routes['loa'].plan_loader(job,self.repo.get_state('cadence',self.cadences['loa'])['native_context'])
        console=self.apps['loa'].direct;effect=payload_hash(plan)
        key=payload_hash({'native_job':job['id'],'effect_key':job['effect_key']})
        record=self.repo.get_state('connectsafely_job',key)
        step=console._canonical_step(console._step(plan,record['preview_id'],1),plan)
        self.csledger.reserve(effect,record['preview_id'],step)
        with self.repo.transaction() as con:
            con.execute("DELETE FROM thunder_state WHERE kind='connectsafely_job' AND entity_id=?",(key,))
        self.now+=timedelta(minutes=1);self.worker().process_one()
        self.assertEqual(self.repo.effect(job['id'])['state'],'needs_reconciliation')
        self.assertEqual(self.cs['loa'].sends,[])

    def test_ygor_pipeline_uses_native_ports(self):
        self.setup_route('ygor')
        email=self.run_job('ygor','E1');self.assertEqual(email['state'],'sent',email)
        with patch.object(self.apps['ygor'].direct.approvals,'approve_pack',side_effect=AssertionError('second approval forbidden')):
            invite=self.run_job('ygor','L1')
        self.assertEqual(invite['state'],'sent',invite)
        self.assertEqual(self.mail.sends[0]['body'],'  Literal E1\n');self.assertEqual(self.cs['ygor'].sends[0]['content'],'')
        self.assertEqual(len(self.repo.list_state('approval')),1)
        self.assertEqual(self.repo.list_state('connectsafely_queue_approval'),[])
        self.assertEqual(len(self.cs['ygor'].sends),1)
        self.assertFalse(self.apps['ygor'].process_one())
        with self.database() as (con,_):self.assertEqual(con.execute('SELECT count(*) FROM toques').fetchone()[0],2)

    def test_loa_pipeline_uses_native_ports(self):
        self.setup_route('loa',full=True)
        invite=self.run_job('loa','L1');self.assertEqual(invite['state'],'sent',invite)
        self.now+=timedelta(days=1)
        dm=self.run_job('loa','L2');self.assertEqual(dm['state'],'sent',dm)
        self.assertEqual(self.cs['loa'].sends[-1]['account_id'],'loa-account')
        self.assertEqual(self.cs['loa'].sends[-1]['content'],'  Literal L2\n')
        self.assertEqual(len(self.repo.list_state('approval')),1)

    def test_missing_native_composition_never_falls_back(self):
        self.setup_route('loa');job=self.dispatch('loa','L1')
        self.assertTrue(self.worker(False).process_one())
        self.assertEqual(self.repo.effect(job['id'])['error_code'],'native_executor_not_configured')
        self.assertEqual(self.cs['loa'].sends,[])
        with self.database() as (con,_):self.assertEqual(con.execute('SELECT count(*) FROM toques').fetchone()[0],0)

    def test_acceptance_account_and_target_matrix(self):
        self.setup_route('loa',full=True);self.assertEqual(self.run_job('loa','L1')['state'],'sent')
        self.now+=timedelta(days=1);job=self.dispatch('loa','L2');executor=self.executor()
        plan=executor.routes['loa'].plan_loader(job,self.repo.get_state('cadence',self.cadences['loa'])['native_context'])
        exact=self.cs['loa'].relationship(plan)
        for patch_value in ({'relationship_status':'INVITATION_SENT'},{'account_id':'ygor-account'},
                            {'recipient_profile_urn':'urn:li:fsd_profile:other'},{'observed_at':self.clock()-61}):
            with self.subTest(patch_value=patch_value):
                self.cs['loa'].observation={**exact,**patch_value}
                with self.assertRaisesRegex(ValueError,'native_connection_required'):executor.prepare_job(job)
        self.cs['loa'].observation=None
        self.assertTrue(self.worker().process_one());self.assertEqual(self.repo.effect(job['id'])['state'],'sent')
        self.assertEqual(len(self.cs['loa'].sends),2)

    def test_email_followup_requires_exact_original_thread(self):
        self.setup_route('ygor',full=True)
        self.assertEqual(self.run_job('ygor','E1')['state'],'sent');self.assertEqual(self.run_job('ygor','L1')['state'],'sent')
        self.now+=timedelta(days=1);job=self.dispatch('ygor','E2');executor=self.executor()
        self.mail.exact=False
        with self.assertRaisesRegex(ValueError,'exact_parent_unsupported'):executor.prepare_job(job)
        self.assertEqual(len(self.mail.sends),1)
        self.mail.exact=True
        self.assertTrue(self.worker().process_one());row=self.repo.effect(job['id']);self.assertEqual(row['state'],'sent',row)
        self.assertEqual(self.mail.sends[-1]['reply_message_id'],'1abc');self.assertEqual(self.mail.sends[-1]['thread_id'],'def123')
        self.assertEqual(self.mail.sends[-1]['body'],'  Literal E2\n')

    def test_timeout_crash_and_restart_do_not_repeat(self):
        self.setup_route('loa');job=self.dispatch('loa','L1');self.cs['loa'].mode='crash'
        with self.assertRaises(Crash):self.worker().process_one()
        self.assertEqual(len(self.cs['loa'].sends),1)
        self.assertEqual(self.repo.list_state('connectsafely_attempt'),[]) # workflow fence rolled back
        with self.database() as (con,_):self.assertEqual(con.execute('SELECT status FROM toques').fetchone()[0],'reservado')
        self.now+=timedelta(seconds=31);self.cs['loa'].mode='confirmed'
        self.assertTrue(self.worker().process_one())
        self.assertEqual(len(self.cs['loa'].sends),1)
        self.assertIn(self.repo.effect(job['id'])['state'],('uncertain','needs_reconciliation'))
        self.worker().process_one();self.assertEqual(len(self.cs['loa'].sends),1)

    def test_reply_stops_both_routes_before_dispatch(self):
        self.setup_route('ygor');self.setup_route('loa',contact='100')
        email=self.dispatch('ygor','E1');invite=self.dispatch('loa','L1')
        reply=ReplyControl(self.repo,self.service.campaign_registry).ingest({
            'id':'manual-stop','channel':'email','content':'Please stop','received_at':self.now.isoformat(),
            'hubspot_contact_id':'100','campaign_id':'s51-ygor','human_reply':True,'identity_verified':True,
            'source':{'provider':'manual','account_id':'ygor','provider_message_id':'stop'}},'ygor')
        self.assertEqual(set(reply['reply']['paused_cadence_ids']),set(self.cadences.values()))
        self.worker().process_one();self.worker().process_one()
        self.assertEqual(self.mail.sends,[]);self.assertEqual(self.cs['loa'].sends,[])
        self.assertEqual(self.repo.effect(email['id'])['state'],'needs_reconciliation');self.assertEqual(self.repo.effect(invite['id'])['state'],'needs_reconciliation')

    def test_gmail_crash_and_timeout_preserve_reservation(self):
        self.setup_route('ygor');job=self.dispatch('ygor','E1');self.mail.mode='crash'
        with self.assertRaises(Crash):self.worker().process_one()
        self.assertEqual(len(self.mail.sends),1)
        with self.database() as (con,_):self.assertEqual(con.execute('SELECT status FROM toques').fetchone()[0],'reservado')
        key=payload_hash({'mailbox':MAILBOX,'cadence':job['payload']['cadence_id'],'step':'ygor-E1'})
        self.assertEqual(self.repo.get_state('gmail_cadence_attempt',key)['phase'],'reserved')
        self.now+=timedelta(seconds=31);self.assertTrue(self.worker().process_one())
        self.assertEqual(len(self.mail.sends),1)
        self.assertIn(self.repo.effect(job['id'])['state'],('uncertain','needs_reconciliation'))

    def test_linkedin_timeout_and_readback_mismatch_retain(self):
        self.setup_route('loa');job=self.dispatch('loa','L1');self.cs['loa'].mode='timeout'
        self.assertTrue(self.worker().process_one())
        self.assertEqual(self.repo.effect(job['id'])['state'],'uncertain')
        self.worker().process_one();self.executor().reconcile_job(self.repo.effect(job['id']))
        self.assertEqual(len(self.cs['loa'].sends),1)
        self.assertFalse(self.apps['loa'].process_one())

    def test_final_callback_revalidates_native_source_and_pause(self):
        self.setup_route('loa');job=self.dispatch('loa','L1')
        # Provider preflight runs after queue+preparation but before callback.
        self.cs['loa'].hook=lambda:self.service.set_global_pause(True,'ygor')
        self.assertTrue(self.worker().process_one())
        self.assertEqual(self.cs['loa'].sends,[])
        self.assertIn(self.repo.effect(job['id'])['state'],('uncertain','needs_reconciliation'))
        with self.database() as (con,_):self.assertEqual(con.execute('SELECT status FROM toques').fetchone()[0],'reservado')

    def test_native_payload_tamper_never_dispatches(self):
        self.setup_route('loa');job=self.dispatch('loa','L1')
        with self.repo.transaction() as con:
            payload=deepcopy(job['payload']);payload['step']['native']['sender_account']='other-account'
            con.execute('UPDATE thunder_effect_jobs SET payload=? WHERE id=?',(json.dumps(payload),job['id']))
        self.assertTrue(self.worker().process_one());self.assertEqual(self.cs['loa'].sends,[])
        with self.database() as (con,_):self.assertEqual(con.execute('SELECT count(*) FROM toques').fetchone()[0],0)

    def test_followup_parent_identity_and_capability_matrix(self):
        self.setup_route('ygor',full=True)
        self.assertEqual(self.run_job('ygor','E1')['state'],'sent');self.assertEqual(self.run_job('ygor','L1')['state'],'sent')
        self.now+=timedelta(days=1);job=self.dispatch('ygor','E2');executor=self.executor()
        original=deepcopy(self.mail.messages['1abc'])
        for mutation in ({'from':'other@example.invalid'},{'to':'other@example.invalid'},
                         {'message_id':'bad-id'}, {'headers_complete':False}, {'body':'other original'}):
            with self.subTest(mutation=mutation):
                self.mail.messages['1abc']={**deepcopy(original),**mutation}
                with self.assertRaises(ValueError):executor.prepare_job(job)
                self.assertEqual(len(self.mail.sends),1)
        self.mail.messages['1abc']=original
        self.mail.exact=False
        with self.assertRaisesRegex(ValueError,'exact_parent_unsupported'):executor.prepare_job(job)
        with self.database() as (con,_):self.assertEqual(con.execute("SELECT count(*) FROM toques WHERE acao='email'").fetchone()[0],1)

    def test_source_change_during_provider_preflight_blocks(self):
        self.setup_route('loa');job=self.dispatch('loa','L1')
        self.cs['loa'].hook=lambda:self.packages.__setitem__('loa.json',self.packages['loa.json']+b' ')
        self.assertTrue(self.worker().process_one())
        self.assertEqual(self.cs['loa'].sends,[])
        self.assertNotEqual(self.repo.effect(job['id'])['state'],'sent')
        self.assertEqual(len(self.repo.list_state('approval')),1)

    def test_route_bound_reader_does_not_swap_accounts(self):
        self.setup_route('loa');job=self.dispatch('loa','L1');executor=self.executor()
        context=self.repo.get_state('cadence',self.cadences['loa'])['native_context']
        reader=executor.reader_for(job,context)
        self.assertEqual(reader.provider_name,'connectsafely');self.assertEqual(reader.account_id,'loa-account')
        self.assertTrue(self.worker().process_one())
        row=self.repo.effect(job['id'])
        self.assertEqual(reader.readback(row['receipt_ref']),{'target':'https://www.linkedin.com/in/s51-person','content':'','effect_id':'loa-L1'})
        self.apps['loa'].direct.account_id='other-account'
        with self.assertRaisesRegex(ValueError,'native_reader_account_changed'):reader.readback(row['receipt_ref'])

    def test_worker_reconciles_uncertain_receipt_without_second_send(self):
        self.setup_route('ygor');job=self.dispatch('ygor','E1');self.mail.mode='timeout'
        self.assertTrue(self.worker().process_one());self.assertEqual(self.repo.effect(job['id'])['state'],'uncertain')
        self.service.set_global_pause(True,'ygor')
        self.mail.mode='confirmed'
        self.assertFalse(self.worker().process_one())
        self.assertEqual(self.repo.effect(job['id'])['state'],'sent')
        self.assertEqual(len(self.mail.sends),1)
        self.assertTrue(self.service.global_paused())
        self.assertEqual(len(self.repo.list_state('approval')),1)

    def test_complete_ygor_route_e3_anchors_e2(self):
        self.setup_route('ygor',full=True)
        for label in ('E1','L1','E2','L2','E3'):
            if label in ('E2','L2','E3'):self.now+=timedelta(days=1)
            row=self.run_job('ygor',label);self.assertEqual(row['state'],'sent',(label,row))
        self.assertEqual(len(self.mail.sends),3);self.assertEqual(len(self.cs['ygor'].sends),2)
        self.assertEqual(self.mail.sends[2]['reply_message_id'],'2abc')
        self.assertEqual(self.mail.sends[1]['reply_message_id'],'1abc')
        self.assertEqual(len(self.repo.list_state('approval')),1)
        with self.database() as (con,_):
            self.assertEqual(con.execute('SELECT count(*) FROM toques').fetchone()[0],5)
            targets=[row[0] for row in con.execute("SELECT alvo FROM toques WHERE canal='email' ORDER BY id")]
        self.assertEqual(targets,['cadence:s51-ygor:e1','cadence:s51-ygor:e2','cadence:s51-ygor:e3'])

    def test_complete_loa_route_uses_one_dispatcher(self):
        self.setup_route('loa',full=True)
        for index,label in enumerate(('L1','L2','L3','L4')):
            if index:self.now+=timedelta(days=1)
            row=self.run_job('loa',label);self.assertEqual(row['state'],'sent',(label,row))
        self.assertEqual(len(self.cs['loa'].sends),4);self.assertEqual(self.mail.sends,[])
        self.assertEqual(self.repo.list_state('connectsafely_queue_approval'),[])
        self.assertFalse(self.apps['loa'].process_one())

    def test_wrong_effect_result_cannot_confirm(self):
        self.setup_route('loa');job=self.dispatch('loa','L1');worker=self.worker()
        with patch.object(worker.native_executor,'execute_job',return_value=ProviderResult('other-step','confirmed','fake:other')):
            self.assertTrue(worker.process_one())
        self.assertEqual(self.repo.effect(job['id'])['state'],'uncertain')
        self.assertIsNone(self.repo.effect(job['id'])['receipt_ref'])
        self.assertEqual(self.cs['loa'].sends,[])

    def test_native_ticket_matches_scoped_bridge_contract(self):
        self.setup_route('ygor',full=True)
        self.assertEqual(self.run_job('ygor','E1')['state'],'sent');self.assertEqual(self.run_job('ygor','L1')['state'],'sent')
        self.now+=timedelta(days=1);self.dispatch('ygor','E2')
        job=self.repo.claim_effect('s51-ticket',self.now,timedelta(seconds=30));executor=self.executor();executor.prepare_job(job)
        policy=executor.technical_policy_for(job)
        key=payload_hash({'mailbox':MAILBOX,'cadence':job['payload']['cadence_id'],'step':'ygor-E2'})
        envelope=self.repo.get_state('gmail_cadence_attempt',key)['envelope']
        self.assertEqual(policy['approval_ref'],payload_hash(self.repo.get_state('approval',job['payload']['cadence_id'])))
        self.assertEqual(set(policy),{'approval_ref','mailbox','to','subject','body_hash','idempotency_key','issued_at','expires_at','max_attempts','thread_id','reply_message_id'})
        script=r"""
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const f=JSON.parse(fs.readFileSync(0,'utf8'));
const {createScopedBridge}=require(path.join(f.bridge,'server.cjs'));
const {EXPECTED,signature}=require(path.join(f.bridge,'contract.cjs'));
const key='s51-offline-synthetic-credential-000000000';let sends=0,received;
const write={supportsExactReplyParent:true,async verifyProfile(){return EXPECTED.email;},async quotaStatus(){return {ready:true};},
 async findDuplicates(){return {complete:true,messages:[]};},async reserveQuota(){},async finishQuota(){},
 async send(mail,{authorize}){authorize();sends++;received=mail;return {id:'abc123',thread_id:mail.thread_id};}};
const read={async verifyProfile(){return EXPECTED.email;}};
(async()=>{
 const bridge=createScopedBridge({readProvider:read,writeProvider:write,credential:key,journalDir:f.journal,policyLoader:()=>f.policy,now:()=>f.now});
 const body={...f.envelope};body.approval_signature=signature(body,key);
 const call=(method,url,body)=>bridge.handle({method,url,body,authorization:'Bearer '+key});
 const status=await call('GET','/v1/status');assert.equal(status.send_enabled,false);assert.equal(status.capabilities.exact_reply_parent,true);
 const result=await call('POST','/v1/send',body);assert.equal(result.status,'sent');
 const replay=await call('POST','/v1/send',body);assert.equal(replay.deduplicated,true);assert.equal(sends,1);
 assert.equal(received.reply_message_id,f.policy.reply_message_id);assert.equal(received.body,f.envelope.body);
 delete f.policy.reply_message_id;delete f.policy.thread_id;
 await assert.rejects(call('POST','/v1/send',body),/scoped_policy_mismatch/);assert.equal(sends,1);
 process.stdout.write(JSON.stringify({sends,read_send_enabled:status.send_enabled,exact_parent:true,replay:true,unanchored_denied:true}));
})().catch(e=>{process.stderr.write(e.message);process.exitCode=1;});
"""
        fixture={'policy':policy,'envelope':envelope,'now':int(self.clock()*1000),
                 'bridge':str(Path(__file__).resolve().parent/'seqd_bridge'),'journal':str((self.root/'bridge-journal').resolve())}
        result=subprocess.run(['node','-e',script],input=json.dumps(fixture),text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'sends':1,'read_send_enabled':False,'exact_parent':True,'replay':True,'unanchored_denied':True})

    def test_native_lease_expiry_before_callback_retains(self):
        self.setup_route('loa');job=self.dispatch('loa','L1')
        self.cs['loa'].hook=lambda:setattr(self,'now',self.now+timedelta(seconds=31))
        self.assertTrue(self.worker().process_one());self.assertEqual(self.cs['loa'].sends,[])
        self.assertIn(self.repo.effect(job['id'])['state'],('uncertain','needs_reconciliation'))
        with self.database() as (con,_):self.assertEqual(con.execute('SELECT status FROM toques').fetchone()[0],'reservado')

    def test_invite_note_remains_literal_without_fabricated_readback(self):
        note='  Exact invitation note\n'
        self.setup_route('loa',invite_note=note);row=self.run_job('loa','L1')
        self.assertEqual(row['state'],'sent');self.assertEqual(self.cs['loa'].sends[0]['content'],note)
        context=self.repo.get_state('cadence',self.cadences['loa'])['native_context']
        reader=self.executor().reader_for(row,context)
        with self.assertRaisesRegex(ValueError,'native_invite_note_readback_unavailable'):reader.readback(row['receipt_ref'])
        self.worker().process_one();self.assertEqual(len(self.cs['loa'].sends),1)

    def test_trusted_calendar_is_propagated_to_worker_service(self):
        self.service._native_calendar['holidays']=['2026-10-29']
        self.setup_route('loa');job=self.dispatch('loa','L1');worker=self.worker()
        self.assertEqual(worker.authorization_service._native_calendar['holidays'],['2026-10-29'])
        self.assertTrue(worker.process_one());self.assertEqual(self.repo.effect(job['id'])['state'],'sent')

    def test_uncertain_reconciliation_is_bounded_per_worker_snapshot(self):
        self.setup_route('loa');self.dispatch('loa','L1');self.cs['loa'].mode='timeout';worker=self.worker()
        self.assertTrue(worker.process_one());self.assertFalse(worker.process_one())
        reads=len(self.cs['loa'].reads)
        self.assertFalse(worker.process_one());self.assertFalse(worker.process_one())
        self.assertEqual(len(self.cs['loa'].reads),reads);self.assertEqual(len(self.cs['loa'].sends),1)

    def test_no_production_composition_or_network(self):
        with self.assertRaisesRegex(AssertionError,'network forbidden'):socket.socket()
        from thunder_os.worker import build_runtime
        import inspect
        self.assertNotIn('NativeCadenceExecutor',inspect.getsource(build_runtime))
        self.assertEqual(self.repo.effects(),[])


if __name__=='__main__':unittest.main()
