"""Bounded account-wide GET observations; no effects or historical full crawl.

Always revisit the head, then at most one saved continuation and three pending
threads. Partial provider windows never advance message checkpoints.
"""
from datetime import datetime
import re
from urllib.parse import quote, urlsplit
from .connectsafely_client import require,urn,ConnectSafelyError
from .models import payload_hash


THREAD_ERRORS={'participants_unverified','account_identity_mismatch','conversation_identity_mismatch',
               'pagination_unverified','provider_schema_invalid','receipt_unverified','message_revision_requires_review'}


def cursor_valid(value):
    return isinstance(value,str) and (bool(re.fullmatch(r'[0-9]{1,16}',value)) or value.startswith('linkedin:') and 9<len(value)<=1024 and all(32<ord(c)<127 for c in value))


class AccountInbox:
    def __init__(self,repository,client,*,start_ms):
        require(type(start_ms) is int and start_ms>0,'pagination_unverified')
        self.repo,self.client,self.start=repository,client,start_ms
        self.key=payload_hash({'account':client.account_id});self.last_from_cache=None

    def _scope(self,data,*,require_echo=False):
        c=self.client
        require(isinstance(data,dict) and data.get('success') is True,'provider_schema_invalid')
        echoes=[]
        for key in ('accountId','linkedInAccountId'):
            if key in data:echoes.append(data[key])
        if 'account' in data:
            require(isinstance(data['account'],dict),'provider_schema_invalid');echoes.append(data['account'].get('id'))
        if 'accounts' in data:
            require(isinstance(data['accounts'],list) and len(data['accounts'])==1 and isinstance(data['accounts'][0],dict),'account_identity_mismatch')
            echoes.append(data['accounts'][0].get('id'))
        require((echoes or not require_echo) and all(x==c.account_id for x in echoes),'account_identity_mismatch')

    def _aliases(self,participants):
        c=self.client;owner=c._profile_identity(c.self_profile_id)
        require(owner is not None and isinstance(participants,list) and 1<=len(participants)<=20,'participants_unverified')
        aliases={owner:owner,c.expected_public_id:owner};identities={owner}
        for p in participants:
            require(isinstance(p,dict),'participants_unverified')
            raw=p.get('profileId');identifier=c._profile_identity(raw)
            require(identifier is not None,'participants_unverified')
            opaque=p.get('participantUrn')
            canonical=c._profile_identity(opaque) if opaque is not None else aliases.get(identifier,identifier)
            require(canonical is not None,'participants_unverified')
            for alias in (identifier,canonical):
                require(alias not in aliases or aliases[alias]==canonical,'participants_unverified');aliases[alias]=canonical
            identities.add(canonical)
        return aliases,identities

    def _list(self,cursor=None):
        query={'accountId':self.client.account_id,'count':10}
        if cursor is not None:query['nextCursor']=cursor
        data=self.client._call('GET','/linkedin/conversations',query);self._scope(data,require_echo=True)
        require('fromCache' not in data or type(data['fromCache']) is bool,'provider_schema_invalid')
        self.last_from_cache=data.get('fromCache')
        rows=data.get('conversations');require(isinstance(rows,list) and len(rows)<=10,'provider_schema_invalid')
        more=data.get('hasMore');next_cursor=data.get('nextCursor')
        coherent=type(more) is bool and 'nextCursor' in data and ((more is False and next_cursor is None) or (more is True and cursor_valid(next_cursor) and bool(rows)))
        projected=[];previous=float('inf');seen=set()
        for row in rows:
            require(isinstance(row,dict) and row.get('linkedInAccountId')==self.client.account_id,'account_identity_mismatch')
            thread=row.get('conversationUrn');require(urn(thread) and row.get('source','linkedin')=='linkedin','unsupported_channel')
            require(thread not in seen,'pagination_unverified');seen.add(thread)
            try:
                stamp=datetime.fromisoformat(row['lastActivityAt'].replace('Z','+00:00'))
                require(stamp.tzinfo is not None,'pagination_unverified');stamp=int(stamp.timestamp()*1000)
            except (KeyError,ValueError,TypeError,AttributeError,OverflowError):raise ConnectSafelyError('pagination_unverified') from None
            require(0<stamp<=int(self.client.clock()*1000)+5000 and stamp<=previous,'pagination_unverified');previous=stamp
            aliases,identities=self._aliases(row.get('participants'))
            latest=row.get('latestMessage');require(isinstance(latest,dict),'provider_schema_invalid')
            require(latest.get('text') is None or isinstance(latest['text'],str) and len(latest['text'])<=8000,'provider_schema_invalid')
            require(type(latest.get('hasAttachment',False)) is bool,'provider_schema_invalid')
            latest_stamp=latest.get('sentAt');require(latest_stamp is None or type(latest_stamp) in (str,int,float),'provider_schema_invalid')
            signature=payload_hash({'activity':stamp,'text':latest.get('text'),'sent_at':latest_stamp,'attachment':latest.get('hasAttachment',False),'identities':sorted(identities)})
            display=[]
            for participant in row['participants']:
                identity=self.client._profile_identity(participant.get('participantUrn') or participant.get('profileId'))
                entry={'identity':aliases.get(identity,identity)}
                name=participant.get('name')
                if isinstance(name,str) and 0<len(name)<=200 and all(ord(ch)>=32 for ch in name):entry['name']=name
                url=participant.get('profileUrl')
                if isinstance(url,str) and len(url)<=500:
                    try:parsed=urlsplit(url)
                    except ValueError:parsed=None
                    if parsed is not None and parsed.scheme=='https' and parsed.netloc in ('www.linkedin.com','linkedin.com') and re.fullmatch(r'/in/[A-Za-z0-9_%.-]+/?',parsed.path) and not parsed.query and not parsed.fragment:entry['profile_url']=url
                display.append(entry)
            projected.append({'display':display,'thread':thread,'activity_ms':stamp,'signature':signature,'aliases':aliases,'identities':sorted(identities)})
        return projected,next_cursor,coherent,more is False

    def _messages(self,thread,checkpoint):
        c=self.client;since=max(self.start,(checkpoint or self.start)-300000)
        data=c._call('GET','/linkedin/conversations/'+quote(thread['thread'],safe='')+'/messages',{'accountId':c.account_id,'count':10,'since':since})
        self._scope(data,require_echo=True)
        require(data.get('conversationUrn')==thread['thread'],'conversation_identity_mismatch')
        participants=data.get('participants');require(isinstance(participants,list),'participants_unverified')
        aliases=thread['aliases'];identities=set(thread['identities']);owner=c._profile_identity(c.self_profile_id)
        for p in participants:
            require(isinstance(p,dict),'participants_unverified')
            identity=c._profile_identity(p.get('profileId'))
            require(identity is not None and aliases.get(identity,identity) in identities,'participants_unverified')
            if p.get('participantUrn') is not None:
                require(c._profile_identity(p['participantUrn'])==aliases.get(identity,identity),'participants_unverified')
        raw=data.get('messages');require(isinstance(raw,list) and len(raw)<=10,'provider_schema_invalid')
        require(type(data.get('hasMore')) is bool and type(data.get('fetchingMore',False)) is bool,'pagination_unverified')
        messages=[];previous=since;seen=set()
        for rawrow in raw:
            row=c._message(rawrow);mid=row.get('messageUrn');require(urn(mid),'receipt_unverified')
            require(mid.startswith('urn:li:messagingMessage:') or bool(re.fullmatch(r'urn:li:msg_message:\(urn:li:fsd_profile:'+re.escape(owner)+r',[A-Za-z0-9_.=-]+\)',mid)),'receipt_unverified')
            require(mid not in seen,'pagination_unverified');seen.add(mid)
            stamp=int(c._seconds(row['sentAt'])*1000)
            require(previous<=stamp<=int(c.clock()*1000)+5000,'pagination_unverified');previous=stamp
            sender=aliases.get(row.get('senderProfileId'),row.get('senderProfileId'));outgoing=row.get('isSentByOwner')
            require(type(outgoing) is bool and sender in identities and (sender==owner)==outgoing,'participants_unverified')
            messages.append({'account_id':c.account_id,'conversation_urn':thread['thread'],'message_urn':mid,
                'sender_profile_id':sender,'sent_at_ms':stamp,'outgoing':outgoing,'content':row.get('text'),
                'body_complete':isinstance(row.get('text'),str),'source':'observed_provider'})
        complete=data['hasMore'] is False and data.get('fetchingMore',False) is False
        return messages,complete

    def poll(self):
        c=self.client;c.verify_identity()
        scan=self.repo.get_state('inbox_account_scan',self.key) or {}
        require(not scan or scan['start_ms']==self.start,'inbox_anchor_changed')
        pending=dict(scan.get('pending',{}));watermark=scan.get('watermark_ms',self.start)
        continuation=scan.get('continuation');floor=scan.get('continuation_floor');visited=list(scan.get('visited',[]))
        cutoff=scan.get('walk_cutoff',max(self.start,watermark-300000)) if continuation else max(self.start,watermark-300000)
        resuming=continuation is not None
        listed=0;overflow=False;coherent=True;terminal=False;displays={}
        # Pin the watermark of a walk: new head bursts belong to the next walk.
        head_max=scan.get('head_max',watermark) if resuming else watermark
        for number in range(2):
            cursor=None if number==0 else continuation
            if number and cursor is None:break
            if cursor is not None and cursor in visited:coherent=False;break
            rows,next_cursor,valid,end=self._list(cursor);listed+=len(rows)
            if number==0 and rows and not resuming:head_max=max(head_max,rows[0]['activity_ms'])
            if number and floor is not None and rows and rows[0]['activity_ms']>floor:valid=False
            if not valid:coherent=False;break
            for row in rows:
                if row['activity_ms']<cutoff:continue
                key=payload_hash({'account':c.account_id,'thread':row['thread']})
                displays[key]={'account_id':c.account_id,'account_public_id':c.expected_public_id,
                    'owner_identity':c._profile_identity(c.self_profile_id),'conversation_urn':row['thread'],
                    'participants':row['display'],'identities':row['identities'],'activity_ms':row['activity_ms'],
                    'source':'observed_provider'}
                stored=self.repo.get_state('inbox_account_thread',key) or {}
                if stored.get('signature')!=row['signature'] or key in pending:
                    if key not in pending and len(pending)>=50:overflow=True;break
                    pending[key]={**row,'attempt_seq':pending.get(key,{}).get('attempt_seq',0),
                                  **({'last_error_code':pending[key]['last_error_code']} if pending.get(key,{}).get('last_error_code') in THREAD_ERRORS else {})}
            if overflow:coherent=False;break
            if end or rows and rows[-1]['activity_ms']<cutoff:
                terminal=True;continuation=None;floor=None;visited=[];break
            if number==0 and continuation is not None:continue
            if cursor is not None:visited.append(cursor)
            if next_cursor in visited or len(visited)>200:coherent=False;break
            continuation=next_cursor;floor=rows[-1]['activity_ms']
        if terminal and coherent:watermark=max(watermark,head_max)
        # Persist discovery before fetching: crash/restart retains bounded work.
        with self.repo.transaction():
            current=self.repo.get_state('inbox_account_scan',self.key) or {}
            require(current==scan,'inbox_checkpoint_changed')
            for display_key,display in displays.items():
                self.repo.put_state('inbox_account_display',display_key,{'display':display,'source_hash':payload_hash(display)})
            self.repo.put_state('inbox_account_scan',self.key,{'start_ms':self.start,'pending':pending,'watermark_ms':watermark,
                'continuation':continuation,'continuation_floor':floor,'visited':visited,'head_max':head_max,'walk_cutoff':cutoff,
                'attempt_seq':scan.get('attempt_seq',0),
                'coverage':'pending_overflow' if overflow else 'partial_listing' if not (terminal and coherent) else 'complete_listing'})
        incoming=observed=duplicates=0
        selected=sorted(pending,key=lambda k:(pending[k].get('attempt_seq',0),k))[:3]
        for key in selected:
            with self.repo.transaction():
                current=self.repo.get_state('inbox_account_scan',self.key)
                sequence=current.get('attempt_seq',0)+1
                pending[key]['attempt_seq']=sequence
                self.repo.put_state('inbox_account_scan',self.key,{**current,'pending':pending,'attempt_seq':sequence})
            thread=pending[key];stored=self.repo.get_state('inbox_account_thread',key) or {}
            try:messages,complete=self._messages(thread,stored.get('checkpoint_ms'))
            except ConnectSafelyError as exc:
                if str(exc) in ('provider_unauthorized','provider_forbidden','provider_rate_limited','provider_outcome_unknown'):raise
                pending[key]['last_error_code']=str(exc) if str(exc) in THREAD_ERRORS else 'provider_schema_invalid'
                pending[key]=pending.pop(key)
                with self.repo.transaction():
                    current=self.repo.get_state('inbox_account_scan',self.key)
                    self.repo.put_state('inbox_account_scan',self.key,{**current,'pending':pending})
                continue
            pending[key].pop('last_error_code',None)
            conflict=False
            with self.repo.transaction():
                for message in messages:
                    mid=payload_hash({'account':c.account_id,'thread':thread['thread'],'message':message['message_urn']})
                    fingerprint=payload_hash(message);old=self.repo.get_state('inbox_account_message',mid)
                    if old:
                        if old['source_hash']==fingerprint:duplicates+=1
                        if old['source_hash']!=fingerprint:
                            conflict=True;revision=mid+':'+fingerprint
                            if not self.repo.get_state('inbox_account_revision',revision):
                                self.repo.append_state('inbox_account_revision',revision,{'message':message,'source_hash':fingerprint,'original_id':mid,'state':'review_required'})
                    else:
                        self.repo.append_state('inbox_account_message',mid,{'message':message,'source_hash':fingerprint})
                        if not message['outgoing']:incoming+=1
                    observed+=1
                checkpoint=stored.get('checkpoint_ms',self.start)
                if complete and not conflict:
                    checkpoint=max(checkpoint,max((m['sent_at_ms'] for m in messages),default=checkpoint))
                    pending.pop(key)
                if key in pending:
                    if conflict:pending[key]['last_error_code']='message_revision_requires_review'
                    pending[key]=pending.pop(key)
                self.repo.put_state('inbox_account_thread',key,{'signature':thread['signature'],'checkpoint_ms':checkpoint,'complete':complete and not conflict})
                current=self.repo.get_state('inbox_account_scan',self.key)
                self.repo.put_state('inbox_account_scan',self.key,{**current,'pending':pending})
        complete=terminal and coherent and not pending
        return {'scope':'account_inbox','account_public_id':c.expected_public_id,'listed_count':listed,
            'from_cache':self.last_from_cache,'provider_freshness':'provider_reports_refreshed' if self.last_from_cache is False else 'cached_or_unknown',
            'thread_blocked_count':sum(p.get('last_error_code') in THREAD_ERRORS for p in pending.values()),
            'blocked_codes':sorted({p['last_error_code'] for p in pending.values() if p.get('last_error_code') in THREAD_ERRORS}),
            'pending_count':len(pending),'duplicates':duplicates,'observed_count':observed,'incoming_count':incoming,'window_complete':complete,
            'watermark_ms':watermark,'lag_seconds':max(0,(int(c.clock()*1000)-min([watermark]+[(self.repo.get_state('inbox_account_thread',k) or {}).get('checkpoint_ms',self.start) for k in pending]))//1000),
            'coverage':'pending_overflow' if overflow else 'complete_response_window' if complete else 'partial_response_window'}
