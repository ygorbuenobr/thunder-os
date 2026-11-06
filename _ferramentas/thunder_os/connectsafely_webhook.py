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
import sqlite3
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

    def _candidates(self,event,*,deadline=None):
        """Find uniquely reserved bindings without trusting event-provided IDs."""
        candidates=[]
        reservation_times={}
        if not event.get('sender_urn'):return candidates
        deadline=min(deadline if deadline is not None else float('inf'),time.monotonic()+1.0)
        with self.repo._connect() as con:
            con.set_progress_handler(lambda: int(time.monotonic()>=deadline),1000)
            try:
                rows=con.execute("SELECT entity_id,version,payload,updated_at FROM thunder_state "
                    "WHERE kind='apollo_linkedin_identity_binding' "
                    "AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.member_urn')=? "
                    "AND json_extract(payload,'$.action_type') IN ('linkedin_step_message','linkedin_step_connect') LIMIT 101",
                    (self.account,event['sender_urn'])).fetchall()
                if len(rows)>100:raise WebhookError('binding_budget_exceeded')
                for row in rows:
                    if time.monotonic()>=deadline:raise WebhookError('binding_budget_exceeded')
                    value=json.loads(row['payload'])
                    if not isinstance(value,dict):raise WebhookError('binding_invalid')
                    binding={**value,'id':row['entity_id'],'version':row['version'],'updated_at':row['updated_at']}
                    reservation=con.execute("SELECT state,created_at FROM apollo_linkedin_reservations "
                        "WHERE company=? AND task_id=? AND account_id=? AND action_type=?",
                        (binding.get('company'),binding.get('task_id'),self.account,binding.get('action_type'))).fetchone()
                    if reservation is None or reservation['state']!='confirmed':continue
                    attempts=con.execute("SELECT 1 FROM thunder_events WHERE entity_id=? "
                        "AND type='apollo_linkedin_campaign_reservation' AND json_extract(payload,'$.binding_id')=? LIMIT 2",
                        (binding.get('task_id'),binding['id'])).fetchall()
                    if len(attempts)==1:
                        candidates.append(binding)
                        reservation_times[binding['id']]=reservation['created_at']
            except sqlite3.OperationalError:
                if time.monotonic()>=deadline:raise WebhookError('binding_budget_exceeded') from None
                raise
            finally:
                con.set_progress_handler(None,0)
        if len(candidates)<=1:return candidates
        # Several touches of the same person are one commercial identity, not
        # an ambiguity. Different tenants, campaigns or contacts still require
        # manual triage. Duplicate evidence for one task is never collapsed.
        fields=('company','campaign_id','contact_id','account_id','member_urn','recipient_public_id')
        identities={tuple(row.get(k) for k in fields) for row in candidates}
        if (len(identities)!=1 or not all(isinstance(candidates[0].get(k),str) and candidates[0][k] for k in fields)
                or len({row.get('task_id') for row in candidates})!=len(candidates)):
            return candidates
        try:
            received=datetime.fromisoformat(event['sent_at'].replace('Z','+00:00'))
            if received.tzinfo is None:return candidates
            ordered=[]
            for binding in candidates:
                started=datetime.fromisoformat(reservation_times[binding['id']].replace('Z','+00:00'))
                if started.tzinfo is None:return candidates
                if started<=received:ordered.append((started,binding['id'],binding))
            return [max(ordered,key=lambda item:(item[0],item[1]))[2]] if ordered else []
        except (KeyError,AttributeError,TypeError,ValueError):
            return candidates

    def process(self,event_id,service):
        """Consume a signed event; ambiguous identities stay in manual triage.

        Must share the exact repository so event and reply commits are atomic.
        Failed processing leaves state received and is safe to retry after restart.
        """
        if getattr(service,'repository',None) is not self.repo:raise WebhookError('shared_repository_required')
        item=self.repo.get_state('connectsafely_event',event_id)
        if not item or item['event']['account_id']!=self.account:raise WebhookError('event_not_found')
        if item['state']!='received':return {'status':item['state'],'event_id':event_id}
        event=item['event']
        # A binding without a confirmed reservation is not proof that the
        # person was reached under that authority. Never guess among campaigns.
        candidates=self._candidates(event)
        if len(candidates)==1:
            from .reply_control import ReplyControl, ReplyControlError
            binding=candidates[0]
            payload={'id':'connectsafely:'+event_id,'channel':'linkedin',
                     'content':event['content'] or '[mensagem sem texto; revisar contexto]',
                     'received_at':event['sent_at'],'hubspot_contact_id':None,
                     'campaign_id':binding['campaign_id'],'human_reply':None,'identity_verified':True,
                     'source':{'provider':'connectsafely','account_id':self.account,
                               'provider_message_id':event['message_urn']}}
            try:
                result=ReplyControl(self.repo,service.campaign_registry).ingest(payload,'verified_source',
                    provider_correlation={'binding_id':binding['id'],'event_id':event_id})
                return {'status':'processed','event_id':event_id,'reply_id':result['reply']['id']}
            except ReplyControlError:
                # A concurrent consumer may have committed first. An invalid
                # binding remains unresolved and follows existing triage.
                current=self.repo.get_state('connectsafely_event',event_id)
                if current and current.get('state')!='received':
                    return {'status':current['state'],'event_id':event_id}
        with self.repo.transaction():
            item=self.repo.get_state('connectsafely_event',event_id)
            if not item or item['event']['account_id']!=self.account:raise WebhookError('event_not_found')
            if item['state']!='received':return {'status':item['state'],'event_id':event_id}
            reply_id='connectsafely:'+event_id
            if not self.repo.get_state('reply',reply_id):
                service.record_reply({'id':reply_id,'channel':'linkedin','content':item['event']['content'] or '[mensagem sem texto; revisar contexto]'},'connectsafely-inbox')
            self.repo.put_state('connectsafely_event',event_id,{**_data(item),'state':'processed','reply_id':reply_id})
            # Keep the existing inbox reply while preserving a durable pointer
            # for later identity correlation. This is not a provider retry and
            # does not claim that the event belongs to any campaign.
            pending=self.repo.get_state('connectsafely_correlation_pending',event_id)
            if pending is None:
                self.repo.append_state('connectsafely_correlation_pending',event_id,
                                       {'account_id':self.account,'event_id':event_id,'state':'pending'})
        return {'status':'processed','event_id':event_id}

    def reconcile_pending(self,service,*,after=None,limit=50):
        """Revisit durable unresolved inbox replies without network or dispatch.

        The returned cursor allows bounded traversal without repeatedly
        selecting the first unresolved events. No timer or retry is started.
        """
        if getattr(service,'repository',None) is not self.repo:raise WebhookError('shared_repository_required')
        if (type(limit) is not int or not 1<=limit<=100
                or after is not None and (not isinstance(after,str) or not re.fullmatch(r'[a-f0-9]{64}',after))):
            raise WebhookError('invalid_reconciliation_cursor')
        deadline=time.monotonic()+5.0
        with self.repo._connect() as con:
            con.set_progress_handler(lambda: int(time.monotonic()>=deadline),1000)
            try:
                rows=con.execute("SELECT entity_id FROM thunder_state WHERE kind='connectsafely_correlation_pending' "
                    "AND entity_id>? AND json_extract(payload,'$.account_id')=? "
                    "AND json_extract(payload,'$.state')='pending' ORDER BY entity_id LIMIT ?",
                    (after or '',self.account,limit+1)).fetchall()
            finally:
                con.set_progress_handler(None,0)
        selected=rows[:limit]
        items=[]
        last=after
        from .reply_control import ReplyControl, ReplyControlError
        control=ReplyControl(self.repo,service.campaign_registry)
        for pending in selected:
            if time.monotonic()>=deadline:
                return {'status':'partial','reason':'time_budget_exhausted','items':items,'next_cursor':last}
            event_id=pending['entity_id']
            item=self.repo.get_state('connectsafely_event',event_id)
            if (not isinstance(item,dict) or item.get('state')!='processed'
                    or not isinstance(item.get('event'),dict) or item['event'].get('account_id')!=self.account):
                items.append({'event_id':event_id,'status':'unverified'})
                last=event_id
                continue
            try:
                candidates=self._candidates(item['event'],deadline=deadline)
            except WebhookError as exc:
                if str(exc)!='binding_budget_exceeded':raise
                # Keep the event pending and visible, but advance this pass so
                # an oversized candidate set cannot starve subsequent events.
                # The worker restarts from the beginning after the last page.
                items.append({'event_id':event_id,'status':'unverified','reason':'binding_budget_exceeded'})
                return {'status':'partial','reason':'binding_budget_exceeded','items':items,'next_cursor':event_id}
            if len(candidates)!=1:
                items.append({'event_id':event_id,'status':'triage'})
                last=event_id
                continue
            try:
                control.promote_connectsafely_event(event_id,candidates[0]['id'])
                items.append({'event_id':event_id,'status':'correlated'})
            except ReplyControlError:
                items.append({'event_id':event_id,'status':'triage'})
            last=event_id
        return {'status':'partial' if len(rows)>limit else 'complete','items':items,
                'next_cursor':selected[-1]['entity_id'] if len(rows)>limit and selected else None}


class ConnectSafelyStoredEventReconciler:
    """Reconcile events already authenticated at ingress, without a secret.

    The worker may read durable event/binding evidence but cannot authenticate
    or accept a new webhook using this object.
    """
    def __init__(self,repository,*,account_id):
        self.repo=repository
        self.account=_text(account_id,200)
        if any(ord(c)<33 for c in self.account):raise WebhookError('invalid_account_configuration')

    _candidates = ConnectSafelyWebhook._candidates
    reconcile_pending = ConnectSafelyWebhook.reconcile_pending
