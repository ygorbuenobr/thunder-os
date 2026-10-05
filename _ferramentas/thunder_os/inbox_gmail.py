"""Gmail read-only protocol and local drafts. No OAuth, HTTP or send implementation.

Requires operator-injected GET transport bound to gmail.readonly and the expected
mailbox. Every run verifies users/me/profile. Message/thread data is untrusted.
"""
import base64
from datetime import datetime,timezone
from email.utils import getaddresses
from typing import Mapping
from urllib.parse import quote
from .models import payload_hash
from .db import RepositoryConflict
from .inbox_common import InboxError,identifier


def plain_body(part,depth=0,budget=None):
    budget=[0] if budget is None else budget
    budget[0]+=1
    if budget[0]>200:raise InboxError('mime_node_limit')
    if not isinstance(part,Mapping) or depth>12:raise InboxError('invalid_mime_tree')
    children=part.get('parts',[])
    if not isinstance(children,list) or len(children)>50:raise InboxError('mime_part_limit')
    body=part.get('body',{})
    if not isinstance(body,Mapping):raise InboxError('invalid_mime_body')
    texts=[];attachments=int(bool(part.get('filename') or body.get('attachmentId')))
    if part.get('mimeType')=='text/plain' and not part.get('filename'):
        data=body.get('data','')
        if not isinstance(data,str) or len(data)>100000:raise InboxError('mail_body_limit')
        try:texts.append(base64.b64decode(data+'='*(-len(data)%4),altchars=b'-_',validate=True).decode('utf-8'))
        except Exception:raise InboxError('mail_body_encoding') from None
    for child in children:
        text,count=plain_body(child,depth+1,budget);texts.append(text);attachments+=count
    text='\n'.join(t for t in texts if t)
    if len(text)>50000:raise InboxError('mail_body_limit')
    return text,attachments


class GmailInbox:
    def __init__(self,service,*,mailbox,get=None):
        if not isinstance(mailbox,str) or mailbox.strip()!=mailbox or mailbox.count('@')!=1:
            raise InboxError('invalid_mailbox')
        self.service,self.repo,self.mailbox,self.get=service,service.repository,mailbox.lower(),get

    def _fetch(self,path,query):
        if self.get is None:raise InboxError('gmail_transport_not_configured')
        try:value=self.get(path,query)
        except Exception:raise InboxError('gmail_read_failed') from None
        if not isinstance(value,Mapping):raise InboxError('invalid_gmail_response')
        return value

    def _message(self,raw,tid):
        if not isinstance(raw,Mapping) or raw.get('threadId')!=tid:raise InboxError('mail_thread_mismatch')
        mid=identifier(raw.get('id'));payload=raw.get('payload',{})
        if not isinstance(payload,Mapping):raise InboxError('invalid_mail_payload')
        headers=payload.get('headers',[])
        if not isinstance(headers,list) or len(headers)>100:raise InboxError('mail_header_limit')
        values={}
        for h in headers:
            if not isinstance(h,Mapping) or not isinstance(h.get('name'),str) or not isinstance(h.get('value'),str) or len(h['value'])>4096:
                raise InboxError('invalid_mail_header')
            name=h['name'].lower()
            if name in ('from','to','cc','subject'):
                if name in values:raise InboxError('duplicate_mail_header')
                values[name]=h['value']
        senders=getaddresses([values.get('from','')]);sender=senders[0][1].lower() if len(senders)==1 else ''
        if sender.count('@')!=1:raise InboxError('ambiguous_mail_sender')
        labels=raw.get('labelIds',[])
        if not isinstance(labels,list) or any(not isinstance(v,str) for v in labels):raise InboxError('invalid_mail_labels')
        outgoing=sender==self.mailbox or 'SENT' in labels or 'DRAFT' in labels
        try:
            stamp=raw['internalDate']
            if not isinstance(stamp,str) or not stamp.isdigit() or len(stamp)>16:raise ValueError()
            date=datetime.fromtimestamp(int(stamp)/1000,timezone.utc).isoformat()
        except Exception:raise InboxError('invalid_mail_date') from None
        content,attachments=plain_body(payload)
        return {'id':mid,'thread_id':tid,'sender':sender,'subject':values.get('subject',''),
                'content':content,'received_at':date,'outgoing':outgoing,'attachments_not_fetched':attachments,
                'body_complete':bool(content.strip()) and attachments==0}

    def sync(self,*,max_pages=5,max_threads=50):
        if type(max_pages)is not int or not 1<=max_pages<=20 or type(max_threads)is not int or not 1<=max_threads<=100:
            raise InboxError('invalid_sync_bound')
        profile=self._fetch('/users/me/profile',{})
        if not isinstance(profile.get('emailAddress'),str) or profile['emailAddress'].lower()!=self.mailbox:raise InboxError('gmail_account_mismatch')
        scope=payload_hash({'provider':'gmail','mailbox':self.mailbox});checkpoint=self.repo.get_state('gmail_sync',scope) or {}
        cursor=checkpoint.get('cursor');seen={cursor} if cursor else set();threads=set()
        listed=set(checkpoint.get('listed_threads',[])) if cursor else set()
        counts={'imported':0,'duplicate':0,'changed':0}
        for _ in range(max_pages):
            page=self._fetch('/users/me/messages',{'q':'in:inbox','maxResults':min(100,max_threads),**({'pageToken':cursor} if cursor else {})})
            entries=page.get('messages',[]);next_cursor=page.get('nextPageToken')
            if not isinstance(entries,list) or len(entries)>100 or (next_cursor is not None and (not isinstance(next_cursor,str) or not next_cursor or len(next_cursor)>2048)):
                raise InboxError('invalid_mail_page')
            if next_cursor and (next_cursor in seen or not entries):raise InboxError('mail_cursor_loop')
            # All page reads/parsing happen before transaction: failure cannot advance cursor.
            batch=[]
            for entry in entries:
                if not isinstance(entry,Mapping):raise InboxError('invalid_mail_list_item')
                tid=identifier(entry.get('threadId'))
                if tid in threads:continue
                if len(threads)>=max_threads:raise InboxError('mail_thread_limit')
                raw=self._fetch('/users/me/threads/'+quote(tid,safe=''),{'format':'full'})
                messages=raw.get('messages',[])
                if raw.get('id')!=tid or not isinstance(messages,list) or not 1<=len(messages)<=100:
                    raise InboxError('incomplete_or_oversize_thread')
                normalized=[self._message(message,tid) for message in messages]
                if len({m['id'] for m in normalized})!=len(normalized):raise InboxError('duplicate_thread_message')
                history=raw.get('historyId')
                if not isinstance(history,str) or not history.isdigit() or len(history)>30:raise InboxError('invalid_thread_history')
                batch.append((tid,normalized,int(history)));threads.add(tid)
                listed.add(payload_hash({'mailbox':self.mailbox,'thread':tid}))
                if len(listed)>10000:raise InboxError('mail_listing_limit')
            with self.repo.transaction():
                current_checkpoint=self.repo.get_state('gmail_sync',scope) or {}
                if current_checkpoint.get('version',0)!=checkpoint.get('version',0):raise RepositoryConflict('mail_sync_changed')
                for tid,messages,history in batch:
                    thread_key=payload_hash({'mailbox':self.mailbox,'thread':tid});thread_hash=payload_hash(messages)
                    existing_thread=self.repo.get_state('gmail_thread',thread_key)
                    if existing_thread and (history<existing_thread['history_id'] or (history==existing_thread['history_id'] and thread_hash!=existing_thread['source_hash'])):
                        raise RepositoryConflict('mail_thread_history_changed')
                    if not existing_thread or history!=existing_thread['history_id'] or existing_thread['source_hash']!=thread_hash or not existing_thread.get('complete'):
                        self.repo.put_state('gmail_thread',thread_key,{'mailbox':self.mailbox,'source_hash':thread_hash,'messages':messages,'complete':True,'history_id':history})
                    for message in messages:
                        if message['outgoing']:continue
                        key='gmail:'+payload_hash({'mailbox':self.mailbox,'message':message['id']});fingerprint=payload_hash(message)
                        old=self.repo.get_state('gmail_message',key)
                        if old:
                            if old['source_hash']==fingerprint:counts['duplicate']+=1;continue
                            rev=key+':'+fingerprint
                            if not self.repo.get_state('gmail_revision',rev):self.repo.append_state('gmail_revision',rev,{'message':message,'source_hash':fingerprint})
                            counts['changed']+=1;continue
                        self.repo.append_state('gmail_message',key,{'mailbox':self.mailbox,'message':message,'thread_key':thread_key,'source_hash':fingerprint})
                        self.service.record_reply({'id':key,'channel':'email','content':message['content'] or '[corpo indisponível; revisar mensagem]'},'gmail-inbox')
                        counts['imported']+=1
                # Absence from a completed inbox listing is not proof of deletion.
                # Keep history but require revalidation before using its draft context.
                if next_cursor is None:
                    for prior in self.repo.list_state('gmail_thread'):
                        if prior['mailbox']==self.mailbox and prior['id'] not in listed and prior.get('complete'):
                            data={k:v for k,v in prior.items() if k not in {'id','version','updated_at'}}
                            self.repo.put_state('gmail_thread',prior['id'],{**data,'complete':False})
                checkpoint=self.repo.put_state('gmail_sync',scope,{'cursor':next_cursor,'complete':next_cursor is None,
                    'listed_threads':sorted(listed) if next_cursor else []})
            cursor=next_cursor
            if not cursor:break
            seen.add(cursor)
        return {**counts,'threads':len(threads),'complete':cursor is None,'external_effects_enabled':False}

    def draft(self,message_id,content,*,actor,expected_thread_hash,expected_version=0):
        if actor!='ygor' or not isinstance(content,str) or not content.strip() or len(content)>10000:
            raise InboxError('invalid_local_draft')
        with self.repo.transaction():
            item=self.repo.get_state('gmail_message',message_id)
            if not item or item['mailbox']!=self.mailbox:raise InboxError('gmail_account_mismatch')
            thread=self.repo.get_state('gmail_thread',item['thread_key'])
            if not thread or thread['source_hash']!=expected_thread_hash:raise RepositoryConflict('mail_thread_changed')
            if not thread.get('complete'):raise InboxError('mail_context_requires_review')
            current=next((m for m in thread['messages'] if m['id']==item['message']['id']),None)
            if current!=item['message'] or not current['body_complete']:raise InboxError('mail_context_requires_review')
            old=self.repo.get_state('gmail_draft',message_id)
            if (old['version'] if old else 0)!=expected_version:raise RepositoryConflict('draft_version_changed')
            return self.repo.put_state('gmail_draft',message_id,{'mailbox':self.mailbox,'reply_to':current['sender'],
                'content':content,'thread_hash':expected_thread_hash,'status':'local_draft','send_authorized':False})
