"""Offline receive-only protocol. No routes, secret discovery, providers or replies.

Docs: connectsafely.ai/docs/webhooks/{security,events,delivery}.
HMAC binds raw body ONLY. The unsigned timestamp window is hygiene, not replay
protection: account/message dedupe is durable and never expires automatically.
Unknown fields (including cookies, browser headers and attachments) are discarded.
"""
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from collections.abc import Mapping
from .models import payload_hash


class WebhookError(ValueError):
    pass


def _pairs(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise WebhookError('duplicate_json_key')
        value[key]=item
    return value


def _text(value,limit,*,empty=False):
    if not isinstance(value,str) or len(value.encode('utf-8'))>limit or ('\x00' in value) or (not empty and not value.strip()):
        raise WebhookError('invalid_event_field')
    return value


def _urn(value,kind,*,optional=False):
    if optional and value in (None,''):return None
    value=_text(value,512)
    if not re.fullmatch('urn:li:'+kind+r':[A-Za-z0-9_:(),.\-]+',value):raise WebhookError('invalid_event_urn')
    return value


def _data(state):
    return {k:v for k,v in state.items() if k not in {'id','version','updated_at'}}


class ConnectSafelyWebhook:
    def __init__(self,repository,*,secret,account_id,clock=time.time,window_seconds=300):
        if not isinstance(secret,bytes) or len(secret)<32:raise WebhookError('webhook_secret_required')
        self.account=_text(account_id,200)
        if any(ord(c)<33 for c in self.account):raise WebhookError('invalid_account_configuration')
        if type(window_seconds)is not int or not 1<=window_seconds<=900:raise WebhookError('invalid_timestamp_window')
        self.repo,self._secret,self.clock,self.window=repository,secret,clock,window_seconds

    def _parse(self,raw,headers):
        if not isinstance(raw,bytes) or not 1<=len(raw)<=200_000:raise WebhookError('webhook_size_limit')
        if not isinstance(headers,Mapping):raise WebhookError('invalid_headers')
        selected={}
        for key,value in headers.items():
            if not isinstance(key,str):raise WebhookError('invalid_headers')
            key=key.lower()
            if key in {'x-webhook-signature','x-webhook-timestamp'}:
                if key in selected or not isinstance(value,str):raise WebhookError('invalid_headers')
                selected[key]=value
        signature=selected.get('x-webhook-signature','')
        if not re.fullmatch(r'sha256=[a-f0-9]{64}',signature):raise WebhookError('invalid_signature')
        expected='sha256='+hmac.new(self._secret,raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):raise WebhookError('invalid_signature')
        stamp=selected.get('x-webhook-timestamp','')
        if not re.fullmatch(r'[0-9]{1,12}',stamp) or abs(self.clock()-int(stamp))>self.window:raise WebhookError('timestamp_outside_window')
        try:
            event=json.loads(raw.decode('utf-8'),object_pairs_hook=_pairs,parse_constant=lambda _: (_ for _ in ()).throw(WebhookError('invalid_json')))
            if not isinstance(event,dict) or event.get('event')!='message.received' or event.get('object')!='event':raise WebhookError('unsupported_event')
            data=event.get('data')
            if not isinstance(data,dict) or data.get('provider')!='LINKEDIN' or data.get('account_id')!=self.account:raise WebhookError('event_account_mismatch')
            urn=_urn(data.get('message_urn'),'messagingMessage')
            if data.get('id',urn)!=urn:raise WebhookError('message_id_mismatch')
            sender=data.get('sender')
            if sender is None:sender={}
            if not isinstance(sender,dict):raise WebhookError('invalid_sender')
            sender_urn=_urn(sender.get('id'),'(?:fsd_profile|member)',optional=True)
            name=sender.get('name')
            if name is not None:_text(name,512,empty=True)
            date=_text(data.get('timestamp'),64)
            parsed=datetime.fromisoformat(date.replace('Z','+00:00'))
            if parsed.tzinfo is None:raise WebhookError('invalid_event_timestamp')
            normalized=parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
            return {'account_id':self.account,'message_urn':urn,'conversation_id':_urn(data.get('conversation_id'),'messagingThread'),
                'sender_urn':sender_urn,'sender_name':name,'profile_url':None,'identity_state':'unresolved',
                'content':_text(data.get('body',''),50_000,empty=True),'sent_at':normalized}
        except WebhookError:raise
        except Exception:raise WebhookError('invalid_event') from None

    def accept(self,raw,headers):
        event=self._parse(raw,headers)
        key=payload_hash({'account':self.account,'message_urn':event['message_urn']})
        fingerprint=payload_hash(event)
        thread=payload_hash({'account':self.account,'conversation':event['conversation_id']})
        future=datetime.fromisoformat(event['sent_at']).timestamp()>self.clock()+self.window
        # Acknowledgement is returned only after this transaction commits.
        with self.repo.transaction():
            existing=self.repo.get_state('connectsafely_event',key)
            if existing:
                if existing['fingerprint']==fingerprint:
                    result={'accepted':True,'status':'duplicate','event_id':key}
                else:
                    revision=key+':'+fingerprint
                    if not self.repo.get_state('connectsafely_revision',revision):
                        self.repo.append_state('connectsafely_revision',revision,{'event':event,'fingerprint':fingerprint,'state':'quarantined'})
                    self.repo.put_state('connectsafely_event',key,{**_data(existing),'state':'quarantined'})
                    result={'accepted':True,'status':'quarantined','event_id':key}
                # Never replace original content or advance thread order on a conflict.
            else:
                self.repo.append_state('connectsafely_event',key,{'event':event,'fingerprint':fingerprint,
                    'state':'quarantined' if future else 'received','review_reason':'future_event_timestamp' if future else None})
                latest=self.repo.get_state('connectsafely_thread',thread)
                if not future and (not latest or event['sent_at']>latest['sent_at']):
                    self.repo.put_state('connectsafely_thread',thread,{'account_id':self.account,'event_id':key,'sent_at':event['sent_at']})
                result={'accepted':True,'status':'quarantined' if future else 'stored','event_id':key}
            if event['sender_urn']:
                sender_key=payload_hash({'account':self.account,'sender_urn':event['sender_urn']})
                seen=self.repo.get_state('connectsafely_sender',sender_key)
                if not seen or event['sent_at']>seen['latest_sent_at']:
                    self.repo.put_state('connectsafely_sender',sender_key,{'account_id':self.account,
                        'sender_urn':event['sender_urn'],'has_reply':True,'latest_sent_at':event['sent_at']})
        return result

    def has_reply(self,account_id,recipient_urn):
        """Positive-only stop signal. False never proves absence at the provider."""
        if account_id!=self.account:raise WebhookError('event_account_mismatch')
        sender=_urn(recipient_urn,'(?:fsd_profile|member)')
        key=payload_hash({'account':self.account,'sender_urn':sender})
        item=self.repo.get_state('connectsafely_sender',key)
        return bool(item and item.get('account_id')==self.account and item.get('sender_urn')==sender and item.get('has_reply') is True)

    def process(self,event_id,service):
        """Optional durable consumer: core reply stays unresolved, never classified.

        Must share the exact repository so event and reply commits are atomic.
        Failed processing leaves state received and is safe to retry after restart.
        """
        if getattr(service,'repository',None) is not self.repo:raise WebhookError('shared_repository_required')
        with self.repo.transaction():
            item=self.repo.get_state('connectsafely_event',event_id)
            if not item or item['event']['account_id']!=self.account:raise WebhookError('event_not_found')
            if item['state']!='received':return {'status':item['state'],'event_id':event_id}
            reply_id='connectsafely:'+event_id
            if not self.repo.get_state('reply',reply_id):
                service.record_reply({'id':reply_id,'channel':'linkedin','content':item['event']['content'] or '[mensagem sem texto; revisar contexto]'},'connectsafely-inbox')
            self.repo.put_state('connectsafely_event',event_id,{**_data(item),'state':'processed','reply_id':reply_id})
        return {'status':'processed','event_id':event_id}
