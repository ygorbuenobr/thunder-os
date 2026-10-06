"""Restricted ConnectSafely protocol port; deliberately has no network/credential loader.

The caller owns approval, durable attempts and no-retry policy. Inject a transport
with (method, path, query, body) -> (HTTP status, decoded JSON). No refresh/sync
POST, fallback channel, account selection or pagination is performed implicitly.
"""
from datetime import datetime
import math
import re
import time
from urllib.parse import quote


class ConnectSafelyError(ValueError):
    """Only static, non-provider diagnostics cross this boundary."""


def require(condition, code):
    if not condition:
        raise ConnectSafelyError(code)


def text(value, maximum=512):
    return isinstance(value, str) and len(value) <= maximum and not any(ord(c)<32 for c in value)


def urn(value):
    return text(value) and value.startswith('urn:li:') and bool(re.fullmatch(r'[A-Za-z0-9_:(),.=-]+', value)) and 'salesNav' not in value


def public_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value))


def select(row, fields):
    require(isinstance(row, dict), 'provider_schema_invalid')
    result = {}
    for field, kind in fields.items():
        if field not in row or row[field] is None:
            continue
        value = row[field]
        require(type(value) is kind and (kind is not str or len(value)<=8000), 'provider_schema_invalid')
        if kind in (int, float):
            require(math.isfinite(value), 'provider_schema_invalid')
        result[field] = value
    return result


ACCOUNT = {k:str for k in ('id','publicId','firstName','lastName','status')}
ACCOUNT.update(enabled=bool, apiSeatAllocated=bool)
PARTICIPANT = {k:str for k in ('profileId','name','headline')}
MESSAGE = {k:str for k in ('messageUrn','text','senderProfileId','senderName')}
MESSAGE.update(isSentByOwner=bool, hasAttachment=bool)


def read_evidence(capability, http_status, payload):
    """Describe one read response only; never grant another capability or a write.

    Profile evidence can also describe a caller-supplied cached response. This
    function performs no request and does not add a profile route/transport.
    """
    require(capability in ('accounts','status','quota','profile'), 'invalid_read_capability')
    result={'capability':capability,'state':'unverified','write_authorized':False}
    require(type(http_status) is int, 'provider_schema_invalid')
    if http_status != 200:
        result.update(state='blocked' if http_status in (401,403,429) else 'unverified',
                      error_code={401:'provider_unauthorized',403:'provider_forbidden',429:'provider_rate_limited'}.get(http_status,'provider_outcome_unknown'))
        return result
    valid=(isinstance(payload,list) if capability=='accounts' else isinstance(payload,dict))
    if capability=='status':valid=valid and payload.get('status') in ('AVAILABLE','IN_USE','ERROR','WARMUP','UNKNOWN')
    if capability=='quota':valid=valid and isinstance(payload.get('quotas'),list)
    if capability=='profile':valid=valid and payload.get('success') is True
    if not valid:return {**result,'error_code':'provider_schema_invalid'}
    result['state']='response_verified'
    if capability=='profile':
        result.update(state='cached_only' if payload.get('cached') is True else 'response_verified',
                      live_fetch_verified=False)
    return result


class ConnectSafelyClient:
    def __init__(self, account_id, expected_public_id, *, request=None, clock=time.time, self_profile_id=None, dm_quota_feature=None):
        require(isinstance(account_id,str) and re.fullmatch(r'[a-f0-9]{24}',account_id), 'account_configuration_invalid')
        require(public_id(expected_public_id), 'account_configuration_invalid')
        self.account_id, self.expected_public_id = account_id, expected_public_id
        require(self_profile_id is None or urn(self_profile_id) or public_id(self_profile_id), 'account_configuration_invalid')
        require(dm_quota_feature is None or isinstance(dm_quota_feature,str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}',dm_quota_feature),'quota_configuration_invalid')
        self.dm_quota_feature=dm_quota_feature
        self.self_profile_id=self_profile_id
        self.request, self.clock = request, clock

    def _call(self, method, path, query=None, body=None):
        require(callable(self.request), 'transport_unconfigured')
        try:
            status, payload = self.request(method, path, query or {}, body)
        except Exception:
            raise ConnectSafelyError('provider_outcome_unknown') from None
        require(type(status) is int,'provider_schema_invalid')
        if status != 200:
            code = {400:'provider_rejected',401:'provider_unauthorized',403:'provider_forbidden',404:'provider_not_found',429:'provider_rate_limited'}.get(status,'provider_outcome_unknown')
            raise ConnectSafelyError(code)
        require(isinstance(payload,(dict,list)), 'provider_schema_invalid')
        return payload

    def accounts(self):
        rows = self._call('GET','/linkedin/accounts',{'q':self.account_id})
        require(isinstance(rows,list) and len(rows)<=100, 'provider_schema_invalid')
        return [select(row,ACCOUNT) for row in rows]

    def verify_identity(self, *, for_write=False):
        rows = self.accounts()
        matches = [r for r in rows if r.get('id')==self.account_id]
        require(len(matches)==1 and sum(r.get('publicId')==self.expected_public_id for r in rows)==1, 'account_identity_mismatch')
        row=matches[0]
        require(row.get('publicId')==self.expected_public_id, 'account_identity_mismatch')
        require(type(for_write) is bool,'invalid_identity_mode')
        if for_write:
            require(row.get('enabled') is True and row.get('status')=='AVAILABLE','account_not_ready')
            # /accounts seat metadata may contradict the account-scoped middleware.
            # Keep it diagnostic; send() still requires current quota + durable
            # dispatch authority. The provider authorizes the single POST itself,
            # and any 401/403 stops without retry or fallback. Reads prove no write.
        return {'verified':True,'account_id':self.account_id,'account_public_id':self.expected_public_id}

    def account_status(self):
        # Seat allocation is only documented on the lightweight accounts endpoint.
        rows=self.accounts();matches=[r for r in rows if r.get('id')==self.account_id]
        require(len(matches)==1 and matches[0].get('publicId')==self.expected_public_id and sum(r.get('publicId')==self.expected_public_id for r in rows)==1,'account_identity_mismatch')
        data=self._call('GET','/linkedin/account/'+self.account_id+'/status')
        result=select(data,{**ACCOUNT,'hasTokens':bool,'lastUsed':str})
        require(result.get('id')==self.account_id and result.get('publicId')==self.expected_public_id,'account_identity_mismatch')
        for key in ('enabled','hasTokens'):require(type(result.get(key)) is bool,'provider_schema_invalid')
        require(result.get('status') in ('AVAILABLE','IN_USE','ERROR','WARMUP','UNKNOWN'),'provider_schema_invalid')
        result['apiSeatAllocated']=matches[0].get('apiSeatAllocated') is True
        result['available']=result['status']=='AVAILABLE' and result['enabled'] and result['hasTokens']
        result['availability_scope']='account_session'
        result['write_authorized']=False
        result['write_entitlement']='reported' if result['apiSeatAllocated'] else 'unverified'
        result['read_evidence']={name:read_evidence(name,200,value) for name,value in [('accounts',rows),('status',data)]}
        return result

    def _directory(self,kind,*,max_pages=3,start_index=0):
        require(kind in ('connections','invitations'),'invalid_route')
        require(type(max_pages) is int and 1<=max_pages<=3 and type(start_index) is int and 0<=start_index<=1_000_000,'invalid_pagination')
        self.verify_identity();output=[];seen=set()
        for _ in range(max_pages):
            query={'accountId':self.account_id,'startIndex':start_index}
            if kind=='connections':query.update(limit=10,sortBy='recentlyAdded')
            data=self._call('GET','/linkedin/'+('connections' if kind=='connections' else 'invitations/sent'),query)
            self._scope(data);require(data.get('accountId')==self.account_id,'account_identity_mismatch')
            rows=data.get(kind)
            require(isinstance(rows,list) and len(rows)<=(10 if kind=='connections' else 100),'provider_schema_invalid')
            require(type(data.get('count')) is int and data['count']==len(rows) and type(data.get('endOfList')) is bool,'pagination_unverified')
            require(type(data.get('startIndex')) is int and data['startIndex']==start_index and type(data.get('nextStartIndex')) is int,'pagination_unverified')
            next_index=data['nextStartIndex'];require(next_index==start_index+len(rows),'pagination_unverified')
            for raw in rows:
                if kind=='connections':
                    item=select(raw,{k:str for k in ('firstName','lastName','vanityName','headline','connectedDate','profileUrl')})
                    from .connection_intake import canonical_profile_slug, profile_key
                    try:identity=canonical_profile_slug(item.get('vanityName'))
                    except ValueError:raise ConnectSafelyError('provider_schema_invalid') from None
                    require(profile_key(item.get('profileUrl'))=='https://www.linkedin.com/in/'+identity,'recipient_identity_mismatch')
                else:
                    item=select(raw,{k:str for k in ('id','invitationId','invitationUrn','memberId','memberUrn','invitationType','customMessage')})
                    require(item.get('invitationType')=='SENT' and isinstance(item.get('invitationUrn'),str) and re.fullmatch(r'urn:li:invitation:[A-Za-z0-9_-]{1,128}',item['invitationUrn']),'provider_schema_invalid')
                    item['profile']=select(raw.get('profile'),{k:str for k in ('firstName','lastName','headline','profileUrl','publicIdentifier')})
                    from .connection_intake import canonical_profile_slug, profile_key
                    try:slug=canonical_profile_slug(item['profile'].get('publicIdentifier'))
                    except ValueError:raise ConnectSafelyError('provider_schema_invalid') from None
                    require(profile_key(item['profile'].get('profileUrl'))=='https://www.linkedin.com/in/'+slug,'recipient_identity_mismatch')
                    stamp=raw.get('sentAt')
                    require(type(stamp) in (str,int,float) and (len(stamp)<=100 if isinstance(stamp,str) else math.isfinite(stamp)),'provider_schema_invalid')
                    item['sentAt']=stamp;identity=item['invitationUrn']
                require(identity not in seen,'pagination_unverified');seen.add(identity);output.append(item)
            if data['endOfList']:
                return {'account_id':self.account_id,kind:output,'complete':True,'next_start_index':None}
            require(next_index>start_index,'pagination_unverified');start_index=next_index
        return {'account_id':self.account_id,kind:output,'complete':False,'next_start_index':start_index}

    def connections(self,*,max_pages=3,start_index=0):
        return self._directory('connections',max_pages=max_pages,start_index=start_index)

    def sent_invitations(self,*,max_pages=3,start_index=0):
        return self._directory('invitations',max_pages=max_pages,start_index=start_index)

    def relationship(self,plan):
        """Account-scoped GET observation; never proof this attempt was sent/accepted."""
        self._plan(plan)
        path='/linkedin/relationship/'+self.account_id+'/'+quote(plan['recipient_public_id'],safe='')
        data=self._call('GET',path)
        require(isinstance(data,dict) and data.get('accountId')==self.account_id,'account_identity_mismatch')
        require(data.get('profileUrn')==plan['recipient_profile_urn'],'recipient_identity_mismatch')
        states={'NOT_CONNECTED':(False,False,False),'CONNECTED':(True,False,False),
                'INVITATION_SENT':(False,True,False),'INVITATION_RECEIVED':(False,False,True)}
        keys=('connected','invitationSent','invitationReceived')
        require(data.get('status') in states and all(type(data.get(k)) is bool for k in keys)
                and tuple(data[k] for k in keys)==states[data['status']],'provider_schema_invalid')
        return {'account_id':self.account_id,'recipient_public_id':plan['recipient_public_id'],
                'recipient_profile_urn':plan['recipient_profile_urn'],'relationship_status':data['status'],
                'observed_at':self.clock()}

    @staticmethod
    def _invitation_time(value):
        # Human labels ("Sent today") and timestamps without a timezone cannot
        # locate an invitation in the dispatched attempt's finite time window.
        if type(value) in (int,float):
            return ConnectSafelyClient._seconds(value)
        if isinstance(value,str):
            try:
                parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                if parsed.tzinfo is not None:return parsed.timestamp()
            except (ValueError,OverflowError):pass
        return 0

    def _reconcile_invitation(self,plan,attempted_at):
        result={'status':'uncertain','error_code':'invite_receipt_unavailable'}
        try:
            self.verify_identity()
            observation=self.relationship(plan)
            result['invitation_observation']=observation
            if observation['relationship_status']!='INVITATION_SENT':return result
            page=self.sent_invitations(max_pages=3)
            if not page['complete']:
                return {**result,'error_code':'history_incomplete'}
            matches=[row for row in page['invitations']
                     if row['profile'].get('publicIdentifier')==plan['recipient_public_id']]
            if len(matches)!=1:return result
            row=matches[0];identity=row.get('invitationId');stamp=self._invitation_time(row.get('sentAt'))
            if (not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identity)
                    or row.get('id')!=identity or row.get('invitationUrn')!='urn:li:invitation:'+identity
                    or row.get('memberUrn')!=plan['recipient_profile_urn']
                    or row.get('memberId')!=plan['recipient_profile_urn'].removeprefix('urn:li:fsd_profile:')
                    or not attempted_at<=stamp<=min(self.clock()+5,attempted_at+120)
                    or ('customMessage' in row and row['customMessage']!=plan['content'])
                    or (plan['content'] and 'customMessage' not in row)):
                return result
            observation={**observation,'invitation_id':identity,'invitation_urn':row['invitationUrn'],
                         'sent_at':stamp,'complete_history':True}
            return {'status':'sent','receipt_ref':'connectsafely:'+row['invitationUrn'],
                    'confirmation_kind':'observed_outbound','write_attribution_confirmed':False,
                    'invitation_observation':observation}
        except (ConnectSafelyError,ValueError):
            # Keep a valid relationship observation even if the receipt page fails.
            return result

    def inbox(self,*,limit=10,max_threads=3):
        require(type(max_threads) is int and 1<=max_threads<=3,'invalid_limit')
        page=self.conversations(limit=limit);rows=page['conversations'];enriched=[]
        for row in rows[:max_threads]:
            messages=self.messages(row['conversationUrn'],limit=limit)
            enriched.append({**row,'messages':messages['messages'],'messages_has_more':messages['has_more']})
        return {'account_id':self.account_id,'conversations':enriched,'has_more':page['has_more'] or len(rows)>max_threads,'complete':not page['has_more'] and len(rows)<=max_threads and all(not r['messages_has_more'] for r in enriched)}

    def quota(self):
        self.verify_identity()
        data=self._call('GET','/linkedin/account/'+self.account_id+'/quota')
        require(isinstance(data,dict) and data.get('accountId')==self.account_id,'account_identity_mismatch')
        rows=data.get('quotas');require(isinstance(rows,list) and len(rows)<=100,'provider_schema_invalid')
        fields={'feature':str,'window':str,'used':int,'limit':int,'remaining':int,'limitReached':bool,'resetAt':str}
        result=[select(row,fields) for row in rows]
        require(all(set(row)==set(fields) for row in result),'quota_contract_unverified')
        return {'account_id':self.account_id,'quotas':result,'read_evidence':read_evidence('quota',200,data)}

    def _quota_allowed(self,action):
        feature='CONNECT' if action=='invite' else self.dm_quota_feature
        require(feature is not None,'quota_contract_unverified')
        rows=[row for row in self.quota()['quotas'] if row['feature']==feature]
        require(rows and len({row['window'] for row in rows})==len(rows),'quota_contract_unverified')
        for row in rows:
            require(row['window'] in ('daily','weekly') and row['used']>=0 and row['limit']>0 and row['remaining']==max(0,row['limit']-row['used']),'quota_contract_unverified')
            try:
                reset=datetime.fromisoformat(row['resetAt'].replace('Z','+00:00'))
                require(reset.tzinfo is not None and reset.timestamp()>self.clock(),'quota_contract_unverified')
            except (ValueError,OverflowError):
                raise ConnectSafelyError('quota_contract_unverified') from None
            require(row['remaining']>0 and row['limitReached'] is False,'quota_exhausted')

    def _scope(self,payload):
        require(isinstance(payload,dict) and payload.get('success') is True,'provider_schema_invalid')
        if 'accountId' in payload:
            require(payload['accountId']==self.account_id,'account_identity_mismatch')
        if 'accounts' in payload:
            require(isinstance(payload['accounts'],list) and len(payload['accounts'])==1 and isinstance(payload['accounts'][0],dict) and payload['accounts'][0].get('id')==self.account_id,'account_identity_mismatch')

    @staticmethod
    def _limit(limit):
        require(type(limit) is int and 1<=limit<=10,'invalid_limit')

    @staticmethod
    def _participants(payload):
        rows=payload.get('participants',[])
        require(isinstance(rows,list) and len(rows)<=20,'provider_schema_invalid')
        return [select(row,PARTICIPANT) for row in rows]

    @staticmethod
    def _profile_identity(value):
        # Only the documented profile namespace is equivalent to its bare ID.
        if isinstance(value,str) and value.startswith('urn:li:fsd_profile:'):
            value=value[len('urn:li:fsd_profile:'):]
        return value if public_id(value) else None

    @classmethod
    def _sender_identity(cls,row):
        values=[row[k] for k in ('senderProfileId','senderId') if row.get(k) is not None]
        nested=row.get('sender')
        require(nested is None or isinstance(nested,dict),'provider_schema_invalid')
        if isinstance(nested,dict) and nested.get('profileId') is not None:
            values.append(nested['profileId'])
        if not values:return None
        identities=[cls._profile_identity(value) for value in values]
        require(None not in identities and len(set(identities))==1,'provider_schema_invalid')
        return identities[0]

    @classmethod
    def _message(cls,row):
        result=select(row,MESSAGE)
        identity=cls._sender_identity(row)
        if identity is not None:result['senderProfileId']=identity
        stamp=row.get('sentAt')
        require(type(stamp) in (int,float) and math.isfinite(stamp) and stamp>0,'provider_schema_invalid')
        result['sentAt']=stamp
        return result

    def conversations(self,limit=10):
        self._limit(limit);self.verify_identity()
        data=self._call('GET','/linkedin/conversations',{'accountId':self.account_id,'count':limit})
        self._scope(data);require(type(data.get('hasMore')) is bool,'pagination_unverified');rows=data.get('conversations')
        require(isinstance(rows,list) and len(rows)<=limit,'provider_schema_invalid')
        result=[]
        for row in rows:
            item=select(row,{'conversationUrn':str,'conversationId':str,'lastActivityAt':str,'unreadCount':int,'source':str})
            require(urn(item.get('conversationUrn')) and item.get('source','linkedin')=='linkedin','unsupported_channel')
            item['participants']=self._participants(row)
            if isinstance(row.get('latestMessage'),dict):
                item['latestMessage']=select(row['latestMessage'],{'text':str,'senderName':str,'hasAttachment':bool})
            result.append(item)
        return {'account_id':self.account_id,'conversations':result,'has_more':data.get('hasMore') is True}

    def messages(self,conversation_urn,limit=10,*,cursor=None,since=None):
        self._limit(limit);require(urn(conversation_urn),'invalid_conversation');self.verify_identity()
        require(cursor is None or isinstance(cursor,str) and re.fullmatch(r'[0-9]{1,16}',cursor),'pagination_unverified')
        require(since is None or type(since) is int and 0<since<10**16,'pagination_unverified')
        require(cursor is None or since is None,'pagination_unverified')
        query={'accountId':self.account_id,'count':limit}
        if cursor is not None:query['cursor']=cursor
        if since is not None:query['since']=since
        data=self._call('GET','/linkedin/conversations/'+quote(conversation_urn,safe='')+'/messages',query)
        self._scope(data);require(type(data.get('hasMore')) is bool,'pagination_unverified')
        if 'conversationUrn' in data:
            require(data['conversationUrn']==conversation_urn,'conversation_identity_mismatch')
        rows=data.get('messages')
        require(isinstance(rows,list) and len(rows)<=limit,'provider_schema_invalid')
        next_cursor=data.get('nextCursor') if 'nextCursor' in data else data.get('cursor')
        require(next_cursor is None or isinstance(next_cursor,str) and re.fullmatch(r'[0-9]{1,16}',next_cursor),'pagination_unverified')
        fetching_more=data.get('fetchingMore',False)
        require(type(fetching_more) is bool,'pagination_unverified')
        return {'account_id':self.account_id,'conversation_urn':conversation_urn,'messages':[self._message(row) for row in rows],'participants':self._participants(data),'has_more':data.get('hasMore') is True,'next_cursor':next_cursor,'fetching_more':fetching_more}

    def _plan(self,plan):
        keys={'account_id','account_public_id','action','recipient_public_id','recipient_profile_urn','channel','messaging_channel','content','conversation_urn'}
        require(isinstance(plan,dict) and not set(plan)-keys and keys-{'conversation_urn'}<=set(plan),'invalid_plan')
        require(plan['account_id']==self.account_id and plan['account_public_id']==self.expected_public_id,'account_identity_mismatch')
        require(plan['channel']=='linkedin' and plan['messaging_channel']=='linkedin_inbox' and plan['action'] in ('dm','invite'),'unsupported_channel')
        require(public_id(plan['recipient_public_id']) and urn(plan['recipient_profile_urn']) and plan['recipient_profile_urn'].startswith('urn:li:fsd_profile:'),'invalid_recipient')
        require(plan['recipient_public_id']!=self.expected_public_id,'invalid_recipient')
        content=plan['content'];require(isinstance(content,str) and '\x00' not in content and not any(0xD800<=ord(c)<=0xDFFF for c in content),'invalid_content')
        require((len(content)<=300 if plan['action']=='invite' else bool(content.strip()) and len(content)<=8000),'invalid_content')
        require(plan.get('conversation_urn') is None or urn(plan['conversation_urn']),'invalid_conversation')
        require(plan['action']=='dm' or plan.get('conversation_urn') is None,'invalid_conversation')

    @staticmethod
    def _seconds(stamp):
        if type(stamp) not in (int,float) or not math.isfinite(stamp):return 0
        return stamp/1000 if stamp>100_000_000_000 else stamp

    def _receipt(self,row,plan,attempted_at):
        require(isinstance(row,dict) and urn(row.get('messageUrn')) and row.get('isSentByOwner') is True,'receipt_unverified')
        value=row['messageUrn'];owner=self._profile_identity(self.self_profile_id)
        modern=bool(owner and re.fullmatch(r'urn:li:msg_message:\(urn:li:fsd_profile:'+re.escape(owner)+r',[A-Za-z0-9_.=-]+\)',value))
        require(value.startswith('urn:li:messagingMessage:') or modern,'receipt_unverified')
        require(row.get('text')==plan['content'] and self._profile_identity(self.self_profile_id) is not None and self._sender_identity(row)==self._profile_identity(self.self_profile_id),'receipt_unverified')
        require(attempted_at<=self._seconds(row.get('sentAt'))<=min(self.clock()+5,attempted_at+120),'receipt_unverified')
        return {'status':'sent','receipt_ref':'connectsafely:'+row['messageUrn'],'message_urn':row['messageUrn'],'account_id':self.account_id,'confirmation_kind':'provider_receipt','write_attribution_confirmed':True}

    def send(self,plan,*,before_dispatch):
        require(callable(before_dispatch),'dispatch_authority_unavailable')
        self._plan(plan);self.verify_identity(for_write=True);attempted_at=self.clock()
        resolved=self._call('GET','/linkedin/conversations/exists/'+quote(plan['recipient_public_id'],safe=''),{'accountId':self.account_id})
        self._scope(resolved)
        require(resolved.get('accountId')==self.account_id and resolved.get('profileUrn')==plan['recipient_profile_urn'],'recipient_identity_mismatch')
        if plan.get('conversation_urn'):
            require(resolved.get('hasConversation') is True and resolved.get('conversationUrn')==plan['conversation_urn'],'conversation_identity_mismatch')
        body={'accountId':self.account_id}
        if plan['action']=='dm':
            body.update(recipientProfileId=plan['recipient_public_id'],recipientProfileUrn=plan['recipient_profile_urn'],messagingChannel='linkedin_inbox',message=plan['content'])
            if plan.get('conversation_urn'):body['conversationUrn']=plan['conversation_urn']
            path='/linkedin/conversations/send'
        else:
            body.update(profileId=plan['recipient_public_id'],profileUrn=plan['recipient_profile_urn'],customMessage=plan['content']);path='/linkedin/connect'
        self._quota_allowed(plan['action'])
        require(before_dispatch() is True,'dispatch_authority_denied')
        try:
            data=self._call('POST',path,body=body)
            self._scope(data)
            require(data.get('accountId')==self.account_id,'receipt_unverified')
            if plan['action']=='invite':return {'status':'uncertain','error_code':'invite_receipt_unavailable'}
            require(data.get('recipientProfileUrn')==plan['recipient_profile_urn'],'receipt_unverified')
            return self._receipt(data.get('sentMessage'),plan,attempted_at)
        except ConnectSafelyError as exc:
            return {'status':'uncertain','error_code':str(exc)}

    def reconcile(self,plan,attempted_at):
        self._plan(plan)
        require(type(attempted_at) in (int,float) and math.isfinite(attempted_at) and 0<attempted_at<=self.clock(),'invalid_attempt_time')
        if plan['action']=='invite':return self._reconcile_invitation(plan,attempted_at)
        # Resolve only an observation; never change the approved plan or hash.
        self.verify_identity()
        resolved=self._call('GET','/linkedin/conversations/exists/'+quote(plan['recipient_public_id'],safe=''),{'accountId':self.account_id})
        self._scope(resolved)
        require(resolved.get('accountId')==self.account_id and resolved.get('profileUrn')==plan['recipient_profile_urn'],'recipient_identity_mismatch')
        thread=resolved.get('conversationUrn')
        if resolved.get('hasConversation') is not True or not urn(thread):return {'status':'uncertain','error_code':'conversation_unresolved'}
        require(plan.get('conversation_urn') is None or plan['conversation_urn']==thread,'conversation_identity_mismatch')
        owner=self._profile_identity(self.self_profile_id);recipient=self._profile_identity(plan['recipient_profile_urn'])
        expected={owner,recipient};aliases={self.expected_public_id:owner,plan['recipient_public_id']:recipient}
        candidates=[];seen=set();cursor=None
        for _ in range(3):
            data=self.messages(thread,cursor=cursor)
            # Public identifiers are bound above by accounts + exact exists lookup.
            ids={aliases.get(r.get('profileId'),self._profile_identity(r.get('profileId'))) for r in data['participants']}
            if None in ids or None in expected or len(data['participants'])!=2 or len(ids)!=2 or ids!=expected:
                return {'status':'uncertain','error_code':'participants_unverified'}
            for row in data['messages']:
                identity=row.get('messageUrn')
                if identity in seen:return {'status':'uncertain','error_code':'pagination_unverified'}
                seen.add(identity)
                if cursor is not None and self._seconds(row.get('sentAt'))*1000>=int(cursor):
                    return {'status':'uncertain','error_code':'pagination_unverified'}
                try:candidates.append(self._receipt(row,plan,attempted_at))
                except ConnectSafelyError:pass
            if data['has_more'] is False:
                if data['fetching_more'] is not False:return {'status':'uncertain','error_code':'pagination_unverified'}
                if len(candidates)!=1:return {'status':'uncertain','error_code':'receipt_missing_or_ambiguous'}
                return {**candidates[0],'write_attribution_confirmed':False,'confirmation_kind':'observed_outbound'}
            next_cursor=data['next_cursor']
            if (not data['messages'] or not isinstance(next_cursor,str) or not re.fullmatch(r'[0-9]{1,16}',next_cursor)
                    or cursor is not None and int(next_cursor)>=int(cursor)
                    or int(next_cursor)!=min(self._seconds(row.get('sentAt'))*1000 for row in data['messages'])):
                return {'status':'uncertain','error_code':'pagination_unverified'}
            cursor=next_cursor
        return {'status':'uncertain','error_code':'pagination_unverified'}
