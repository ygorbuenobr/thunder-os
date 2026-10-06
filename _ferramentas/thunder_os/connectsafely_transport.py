"""Opt-in HTTPS transport. No environment credential lookup or automatic composition.

The client remains disconnected by default. Connection factory and credential
loader are explicit boundaries; tests inject both and make zero network calls.
"""
import http.client
import json
import os
import re
import ssl
from urllib.parse import urlencode, quote, unquote
from .connectsafely_client import ConnectSafelyError, require, urn

HOST='api.connectsafely.ai'
MAX_RESPONSE=1_000_000


class ConnectSafelyHTTPS:
    def __init__(self, account_id, credential, *, connection_factory=None):
        require(isinstance(account_id,str) and re.fullmatch(r'[a-f0-9]{24}',account_id),'account_configuration_invalid')
        require(callable(credential),'credential_unconfigured')
        self.account_id,self.credential=account_id,credential
        self.factory=connection_factory or http.client.HTTPSConnection

    def _route(self,method,path,query,body):
        require(isinstance(query,dict),'invalid_route')
        if method=='GET':
            require(body is None,'invalid_route')
            if path=='/linkedin/accounts':
                require(query=={'q':self.account_id},'invalid_route');return
            if path=='/linkedin/account/'+self.account_id+'/status':
                require(not query,'invalid_route');return
            if path in ('/linkedin/connections','/linkedin/invitations/sent'):
                allowed={'accountId','startIndex'} | ({'limit','sortBy'} if path=='/linkedin/connections' else set())
                require(set(query)==allowed and query.get('accountId')==self.account_id and type(query.get('startIndex')) is int and 0<=query['startIndex']<=1_000_000,'invalid_route')
                if path=='/linkedin/connections':require(query.get('sortBy')=='recentlyAdded' and type(query.get('limit')) is int and query['limit']==10,'invalid_route')
                return
            if path=='/linkedin/account/'+self.account_id+'/quota':
                require(not query,'invalid_route');return
            if re.fullmatch(r'/linkedin/relationship/'+re.escape(self.account_id)+r'/[A-Za-z0-9_-]{1,100}',path):
                require(query=={},'invalid_route');return
            if path=='/linkedin/profile':
                from .connection_intake import canonical_profile_slug, ConnectionIntakeError
                profile_id=query.get('profileId')
                try: valid=canonical_profile_slug(profile_id)==profile_id
                except ConnectionIntakeError: valid=False
                require(set(query)=={'accountId','profileId'} and query.get('accountId')==self.account_id
                        and valid,'invalid_route')
                return
            allowed={'accountId','count'}
            if path=='/linkedin/conversations':
                allowed.add('nextCursor')
                if 'nextCursor' in query:
                    from .connectsafely_account_inbox import cursor_valid
                    require(cursor_valid(query['nextCursor']),'invalid_route')
            elif re.fullmatch(r'/linkedin/conversations/exists/[A-Za-z0-9_-]{1,100}',path):allowed={'accountId'}
            elif re.fullmatch(r'/linkedin/conversations/[A-Za-z0-9_%.,()=-]+/messages',path):
                encoded=path.split('/')[3];decoded=unquote(encoded)
                require(urn(decoded) and quote(decoded,safe='')==encoded,'invalid_route')
                allowed.update(('cursor','since'))
                require(not ('cursor' in query and 'since' in query),'invalid_route')
                if 'since' in query:require(type(query['since']) is int and 0<query['since']<10**16,'invalid_route')
                if 'cursor' in query:require(isinstance(query['cursor'],str) and re.fullmatch(r'[0-9]{1,16}',query['cursor']),'invalid_route')
            else:raise ConnectSafelyError('invalid_route')
            require(not set(query)-allowed and query.get('accountId')==self.account_id,'invalid_route')
            if 'count' in allowed:require(type(query.get('count')) is int and 1<=query['count']<=10,'invalid_route')
            return
        require(method=='POST' and not query and isinstance(body,dict),'invalid_route')
        allowed = {'accountId','recipientProfileId','recipientProfileUrn','conversationUrn','message','messagingChannel'} if path=='/linkedin/conversations/send' else {'accountId','profileId','profileUrn','customMessage'} if path=='/linkedin/connect' else set()
        require(allowed and not set(body)-allowed and body.get('accountId')==self.account_id,'invalid_route')
        if path.endswith('/send'):require(body.get('messagingChannel')=='linkedin_inbox','invalid_route')

    def __call__(self,method,path,query,body):
        self._route(method,path,query,body)
        require(not os.environ.get('SSL_CERT_FILE') and not os.environ.get('SSL_CERT_DIR'),'tls_configuration_rejected')
        connection=None
        try:
            credential=self.credential()
            require(isinstance(credential,str) and 16<=len(credential)<=4096 and re.fullmatch(r'[A-Za-z0-9._~-]+',credential),'credential_unavailable')
            connection=self.factory(HOST,443,timeout=15,context=ssl.create_default_context())
            wire=json.dumps(body,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode() if body is not None else None
            require(wire is None or len(wire)<=40000,'request_too_large')
            connection.request(method,path+('?' + urlencode(query) if query else ''),body=wire,headers={'Authorization':'Bearer '+credential,'Accept':'application/json','Content-Type':'application/json'})
            credential=None
            response=connection.getresponse()
            require(type(response.status) is int,'provider_schema_invalid')
            if response.status!=200:return response.status,{} # Never pass provider error text.
            raw=response.read(MAX_RESPONSE+1)
            require(len(raw)<=MAX_RESPONSE,'response_too_large')
            def unique(pairs):
                obj={}
                for key,value in pairs:
                    require(key not in obj,'provider_schema_invalid');obj[key]=value
                return obj
            def reject_constant(_):raise ConnectSafelyError('provider_schema_invalid')
            result=json.loads(raw,object_pairs_hook=unique,parse_constant=reject_constant)
            require(isinstance(result,(dict,list)),'provider_schema_invalid')
            return 200,result
        except ConnectSafelyError as exc:
            allowed={'credential_unavailable','request_too_large','provider_schema_invalid','response_too_large'}
            raise ConnectSafelyError(str(exc) if str(exc) in allowed else 'provider_outcome_unknown') from None
        except Exception:
            raise ConnectSafelyError('provider_outcome_unknown') from None
        finally:
            if connection:
                try:connection.close()
                except Exception:pass
