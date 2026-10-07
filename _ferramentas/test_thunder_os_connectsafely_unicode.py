"""Unicode recipient regression: synthetic identities, no external IO."""
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import Mock
from thunder_os.connectsafely_client import canonical_recipient_id, recipient_id, ConnectSafelyError
from thunder_os.connectsafely_console import ConnectSafelyConsole
from thunder_os.connectsafely_read_targets import normalize_target_url, ReadTargetResolver
from thunder_os.connectsafely_ledger import ConnectSafelyLedger
from thunder_os.db import Repository
from test_thunder_os_connectsafely_client import ClientTests, PLAN, ACCOUNT, RECIPIENT
from test_thunder_os_connectsafely_transport import TransportTests

SLUG='hélio-fixture'

class UnicodeIdentityTests(unittest.TestCase):
    def test_url_and_plan_have_one_review_and_ledger_identity(self):
        console=ConnectSafelyConsole(None,None,None,account_id=ACCOUNT,account_public_id='ygorbuenobr')
        plans=[]
        for slug in (SLUG,quote(SLUG,safe='')):
            for host in ('linkedin.com','www.linkedin.com','br.linkedin.com'):
                self.assertEqual('https://www.linkedin.com/in/'+SLUG+'/',normalize_target_url('https://'+host+'/in/'+slug,profile=True))
            plans.append(console._plan({**PLAN,'action':'invite','conversation_urn':None,'content':'','recipient_public_id':slug}))
        self.assertEqual(plans[0],plans[1])
        steps=[console._canonical_step(console._step(p,'fixture',1),p) for p in plans]
        self.assertEqual(ConnectSafelyLedger._scope(steps[0],'one'),ConnectSafelyLedger._scope(steps[1],'two'))
        self.assertIn(SLUG,steps[0]['recipient'])
        self.assertNotEqual(canonical_recipient_id('straße'),canonical_recipient_id('strasse'))
        self.assertEqual('fixture-ascii',canonical_recipient_id('Fixture-ASCII'))

    def test_unicode_duplicate_reservation_uses_same_canonical_touch(self):
        import sqlite3
        from contextlib import contextmanager
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'ledger.db'
            @contextmanager
            def database():
                con=sqlite3.connect(path)
                try:
                    yield con,'?';con.commit()
                except Exception:
                    con.rollback();raise
                finally:con.close()
            with database() as (con,_):
                con.execute('CREATE TABLE thunder_suppressions(canal TEXT,pessoa_chave TEXT,reason TEXT)')
                con.execute('CREATE TABLE respostas(id INTEGER,canal TEXT,pessoa_chave TEXT)')
                con.execute('CREATE TABLE toques(id INTEGER PRIMARY KEY,canal TEXT,acao TEXT,pessoa_chave TEXT,alvo TEXT,status TEXT,provider_id TEXT,detalhe TEXT,agente TEXT,ref TEXT)')
                con.execute('CREATE TABLE thunder_effect_touches(effect_key TEXT PRIMARY KEY,touch_id INTEGER)')
            ledger=ConnectSafelyLedger(db_context=database)
            console=ConnectSafelyConsole(None,None,None,account_id=ACCOUNT,account_public_id='ygorbuenobr')
            for i,slug in enumerate((SLUG,quote(SLUG,safe=''))):
                plan=console._plan({**PLAN,'action':'invite','conversation_urn':None,'content':'','recipient_public_id':slug})
                step=console._canonical_step(console._step(plan,'preview'+str(i),1),plan)
                created,touch=ledger.reserve('effect'+str(i),'preview'+str(i),step)
                self.assertEqual(i==0,created)
                self.assertEqual('1',touch['id'])
            with database() as (con,_):self.assertEqual(1,con.execute('SELECT COUNT(*) FROM toques').fetchone()[0])

    def test_social_author_validation_stays_restricted(self):
        import re
        from thunder_os.connectsafely_read_targets import PROFILE_ID
        for slug in ('bad space','bad%value','bad?value','bad#value','bad\nvalue'):
            self.assertIsNone(re.fullmatch(PROFILE_ID,slug))

    def test_malformed_unicode_aliases_and_urls_rejected(self):
        for slug in ('h%2Felio','h%252Felio','h%C0%AFelio','h%FFelio','h%00elio','h%20elio','h%3Felio','h%23elio','h%','K-test','İ-test','he\u0301lio'):
            with self.subTest(slug=slug),self.assertRaises(ConnectSafelyError):canonical_recipient_id(slug)
        for url in ('http://linkedin.com/in/'+SLUG,'https://evil.invalid/in/'+SLUG,'https://user@linkedin.com/in/'+SLUG,'https://linkedin.com:443/in/'+SLUG,'https://linkedin.com/in/'+SLUG+'/extra'):
            with self.subTest(url=url),self.assertRaises(ConnectSafelyError):normalize_target_url(url,profile=True)

class UnicodeTransportTests(TransportTests):
    def test_unicode_get_routes_encode_once_and_reject_ambiguous_wire_paths(self):
        self.raw=b'{}'
        for prefix,query in (('/linkedin/relationship/'+ACCOUNT+'/',{}),('/linkedin/conversations/exists/',{'accountId':ACCOUNT})):
            path=prefix+quote(SLUG,safe='')
            self.transport('GET',path,query,None)
            self.assertEqual(path+(('?accountId='+ACCOUNT) if query else ''),self.calls[-1][0][1])
            before=len(self.created)
            for suffix in (SLUG,quote(quote(SLUG,safe=''),safe=''),'h%2Felio','h%C0%AFelio','h%FFelio','%68elio','h%c3%a9lio','../other'):
                with self.assertRaises(ConnectSafelyError):self.transport('GET',prefix+suffix,query,None)
            self.assertEqual(before,len(self.created))

class UnicodeClientTests(ClientTests):
    def unicode_plan(self):return {**PLAN,'action':'invite','conversation_urn':None,'content':'','recipient_public_id':SLUG}
    def test_unicode_resolve_plan_and_timeout_one_write(self):
        original=self.request
        def request(method,path,query,body):
            if path=='/linkedin/profile':
                self.calls.append((method,path,query,body))
                return 200,dict(success=True,accountId=ACCOUNT,profileId=SLUG,cached=True,profile={'publicIdentifier':SLUG,'entityUrn':RECIPIENT,'firstName':'Synthetic'})
            return original(method,path,query,body)
        self.client.request=request
        with tempfile.TemporaryDirectory() as temp:
            resolver=ReadTargetResolver(Repository(Path(temp)/'fixture.db'),self.client,clock=self.client.clock)
            result=resolver({'action':'invite','profile_url':'https://br.linkedin.com/in/'+quote(SLUG,safe='')})
            self.assertTrue(result['resolved']);self.assertEqual(SLUG,result['target']['recipient_public_id'])
        self.post=TimeoutError('synthetic timeout')
        result=self.client.send(self.unicode_plan(),before_dispatch=lambda:True)
        self.assertEqual('uncertain',result['status']);self.assertEqual(1,sum(c[0]=='POST' for c in self.calls))
        self.assertIn('/linkedin/conversations/exists/'+quote(SLUG,safe=''),[c[1] for c in self.calls])
        self.assertEqual(SLUG,next(c[3]['profileId'] for c in self.calls if c[0]=='POST'))

    def test_unicode_account_and_urn_mismatch_never_dispatch(self):
        original=self.request
        for field,value in [('accountId','b'*24),('profileUrn','urn:li:fsd_profile:OTHER')]:
            self.calls=[]
            def request(method,path,query,body):
                code,data=original(method,path,query,body)
                if '/exists/' in path:data={**data,field:value}
                return code,data
            self.client.request=request
            with self.assertRaises(ConnectSafelyError):self.client.send(self.unicode_plan(),before_dispatch=lambda:True)
            self.assertFalse(any(c[0]=='POST' for c in self.calls))

    def test_connected_and_pending_unicode_observation_is_read_only(self):
        for status,flags in [('CONNECTED',(True,False,False)),('INVITATION_SENT',(False,True,False)),('INVITATION_RECEIVED',(False,False,True))]:
            seen=[]
            def request(method,path,query,body):
                seen.append((method,path))
                return 200,dict(accountId=ACCOUNT,profileUrn=RECIPIENT,status=status,**dict(zip(['connected','invitationSent','invitationReceived'],flags)))
            self.client.request=request
            self.assertEqual(status,self.client.relationship(self.unicode_plan())['relationship_status'])
            self.assertEqual([('GET','/linkedin/relationship/'+ACCOUNT+'/'+quote(SLUG,safe=''))],seen)

if __name__=='__main__':unittest.main()
