import os
import unittest
from unittest.mock import patch
from thunder_os.connectsafely_transport import ConnectSafelyHTTPS,MAX_RESPONSE
from thunder_os.connectsafely_client import ConnectSafelyError
from thunder_os.connectsafely_read_targets import TargetReadHTTPS, normalize_target_url

class TransportTests(unittest.TestCase):
    def test_target_selection_accepts_only_confirmed_curly_apostrophe(self):
        self.assertEqual('https://www.linkedin.com/in/d\u2019angelo/',
                         normalize_target_url('https://linkedin.com/in/d\u2019angelo', profile=True))
        for slug in ('d\u201cangelo', 'd%2Fangelo', '../other'):
            with self.assertRaisesRegex(ConnectSafelyError, 'invalid_target_request'):
                normalize_target_url('https://linkedin.com/in/' + slug, profile=True)
        target = TargetReadHTTPS('a'*24, lambda: 'synthetic-token-only', connection_factory=self.factory)
        self.raw = b'{}'
        target('GET', '/linkedin/profile', {'accountId': 'a'*24, 'profileId': 'd\u2019angelo'}, None)
        self.assertIn('profileId=d%E2%80%99angelo', self.calls[-1][0][1])
        before = len(self.created)
        with self.assertRaisesRegex(ConnectSafelyError, 'invalid_route'):
            target('GET', '/linkedin/profile', {'accountId': 'a'*24, 'profileId': 'd\u201cangelo'}, None)
        self.assertEqual(before, len(self.created))

    def setUp(self):
        self.calls=[];self.closed=0;self.status=200;self.raw=b'[]';self.created=[]
        self.transport=ConnectSafelyHTTPS('a'*24,lambda:'synthetic-token-only',connection_factory=self.factory)
    def factory(self,*args,**kwargs):self.created.append((args,kwargs));return self
    def request(self,*args,**kwargs):self.calls.append((args,kwargs))
    def getresponse(self):return self
    def read(self,n):return self.raw[:n]
    def close(self):self.closed+=1
    def call(self):return self.transport('GET','/linkedin/accounts',{'q':'a'*24},None)
    def test_read_and_write_use_one_bearer_prefix_same_loader(self):
        loads=[]
        def credential():loads.append(True);return 'synthetic-token-only'
        self.transport.credential=credential
        self.call()
        self.transport('POST','/linkedin/conversations/send',{},dict(accountId='a'*24,messagingChannel='linkedin_inbox',message='fixture'))
        self.assertEqual(len(loads),2)
        self.assertEqual([c[1]['headers']['Authorization'] for c in self.calls],['Bearer synthetic-token-only']*2)
    def test_prefixed_or_whitespace_credential_never_opens_connection(self):
        for value in ('Bearer synthetic-token-only',' synthetic-token-only','synthetic-token-only\n','synthetic token only'):
            self.transport.credential=lambda:value
            with self.assertRaisesRegex(ConnectSafelyError,'credential_unavailable'):self.call()
        self.assertEqual(self.created,[])

    def test_conversation_nextcursor_verbatim_only_list_route(self):
        self.raw=b'{}'
        for cursor in ('1790815266878','linkedin:opaque+/='):
            self.transport('GET','/linkedin/conversations',{'accountId':'a'*24,'count':10,'nextCursor':cursor},None)
            from urllib.parse import parse_qs,urlsplit
            path=self.calls[-1][0][1]
            self.assertEqual(parse_qs(urlsplit(path).query)['nextCursor'],[cursor])
        before=len(self.created)
        for cursor in ('bad','linkedin:',{'cookie':'SECRET'},'linkedin:bad\n'):
            with self.assertRaises(ConnectSafelyError):
                self.transport('GET','/linkedin/conversations',{'accountId':'a'*24,'count':10,'nextCursor':cursor},None)
        with self.assertRaises(ConnectSafelyError):
            self.transport('GET','/linkedin/conversations/urn%3Ali%3Amsg_conversation%3ATHREAD/messages',{'accountId':'a'*24,'count':10,'nextCursor':'123'},None)
        self.assertEqual(len(self.created),before)
    def test_relationship_is_get_only_exact_account_and_no_query(self):
        self.raw=b'{}';route='/linkedin/relationship/'+'a'*24+'/fixture-person'
        self.transport('GET',route,{},None)
        before=len(self.created)
        for method,path,query,body in [('POST',route,{},{}),('GET',route,{'accountId':'a'*24},None),
                                      ('GET',route.replace('a'*24,'b'*24),{},None),
                                      ('GET','/linkedin/relationship/fixture-person',{},None),
                                      ('GET',route+'/extra',{},None)]:
            with self.assertRaisesRegex(ConnectSafelyError,'invalid_route'):
                self.transport(method,path,query,body)
        self.assertEqual(before,len(self.created))

    def test_pinned_host_tls_and_no_proxy(self):
        with patch.dict(os.environ,{'HTTPS_PROXY':'http://bad.invalid'},clear=True):self.assertEqual(self.call(),(200,[]))
        self.assertEqual(self.created[0][0],('api.connectsafely.ai',443));self.assertEqual(self.created[0][1]['timeout'],15)
        self.assertTrue(self.created[0][1]['context'].check_hostname);self.assertEqual(self.closed,1)
    def test_denial_redirect_no_follow_or_error_leak(self):
        for status in [301,302,401,403,429,500]:
            self.status=status;self.raw=b'{"error":"SECRET"}';self.calls=[]
            self.assertEqual(self.call(),(status,{}));self.assertEqual(len(self.calls),1)
    def test_malformed_oversize_duplicate_json_rejected(self):
        for raw in [b'x'*(MAX_RESPONSE+1),b'bad',b'{"a":1,"a":2}',b'{"n":NaN}']:
            self.raw=raw
            with self.assertRaises(ConnectSafelyError):self.call()
    def test_dangerous_route_account_channel_block_before_factory(self):
        for method,path,query,body in [('POST','/linkedin/conversations/sync',{},{}),('GET','https://evil.invalid',{},None),('GET','/linkedin/accounts',{'q':'b'*24},None),('POST','/linkedin/conversations/send',{},dict(accountId='a'*24,messagingChannel='auto'))]:
            with self.assertRaisesRegex(ConnectSafelyError,'route'):self.transport(method,path,query,body)
        self.assertEqual(self.created,[])
    def test_profile_lookup_is_get_only_and_account_bound(self):
        self.raw=b'{}'
        self.transport('GET','/linkedin/profile',
                       {'accountId':'a'*24,'profileId':'person-one'},None)
        self.assertIn('profileId=person-one',self.calls[-1][0][1])
        self.transport('GET','/linkedin/profile',
                       {'accountId':'a'*24,'profileId':'d’angelo'},None)
        self.assertIn('profileId=d%E2%80%99angelo',self.calls[-1][0][1])
        before=len(self.created)
        for query in ({'accountId':'b'*24,'profileId':'person-one'},
                      {'accountId':'a'*24,'profileId':'../other'},
                      {'accountId':'a'*24,'profileId':'person-one','forceRefresh':True}):
            with self.assertRaisesRegex(ConnectSafelyError,'invalid_route'):
                self.transport('GET','/linkedin/profile',query,None)
        self.assertEqual(before,len(self.created))
    def test_custom_ca_rejected_and_exception_sanitized(self):
        with patch.dict(os.environ,{'SSL_CERT_FILE':'not-read'}):
            with self.assertRaisesRegex(ConnectSafelyError,'tls_configuration'):self.call()
        def broken():raise RuntimeError('SECRET')
        self.transport.credential=broken
        with self.assertRaisesRegex(ConnectSafelyError,'^provider_outcome_unknown$'):self.call()

if __name__=='__main__':unittest.main()
