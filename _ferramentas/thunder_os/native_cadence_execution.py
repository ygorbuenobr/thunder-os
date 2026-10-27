"""Trusted native bridge into the existing Gmail and CS execution protocols.

No production factory installs this composition. Source packages carry data only;
constructor-injected ports own route identity, current reads and dispatch. Unknown
outcomes retain their reservations and never acquire automatic retry authority.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
import time
import hashlib
import re

from .models import payload_hash
from .approvals import _step_hash
from .native_contract import validate_native_context, validate_native_step
from .native_cadence import validate_readers, DEFAULT_CALENDAR
from .providers.result import ProviderResult
from .thunder_email_runtime import GmailCadenceAdapter, require
from .email_readback import GmailReceiptReader
from .seqd_client import MAILBOX


@dataclass(frozen=True)
class NativeRoute:
    """Constructor-only ports, never accepted from a request or source package."""
    connectsafely_app: object = None
    plan_loader: object = None
    gmail_adapter: object = None
    relationship_reader: object = None


class NativeEffectReader:
    """Per-job reader; one route/account remains bound for its whole lifetime."""
    def __init__(self, executor, job, context):
        self._executor, self._job, self._context = executor, deepcopy(job), deepcopy(context)
        binding = executor.binding_for(job,context)
        self.provider_name, self.account_id = binding['provider'], binding['account_id']

    def _scope(self):
        binding = self._executor.binding_for(self._job,self._context)
        require(binding == {'provider':self.provider_name,'account_id':self.account_id}, 'native_reader_account_changed')
        return self._executor._scope(self._job,self._context)

    def read_message(self, receipt, *, reply_metadata=False):
        _, _, step, route = self._scope()
        require(step['native']['action'] == 'email','native_reader_channel_mismatch')
        return GmailReceiptReader(self._executor.repo,route.gmail_adapter.client).read_message(receipt,reply_metadata=reply_metadata)

    def readback(self, receipt):
        _, context, step, route = self._scope()
        if step['native']['action'] == 'email':
            parent = self._executor._parent(self._job,step,route)
            observed = self._executor._email_readback(self._job,receipt,parent)
            return {'target':observed['to'],'content':observed['content'],'subject':observed['subject'],'effect_id':step['id']}
        plan = self._executor._plan(self._job,context,step,route)
        require(plan['action'] != 'invite' or plan['content'] == '', 'native_invite_note_readback_unavailable')
        attempt = self._executor.repo.get_state('connectsafely_attempt',payload_hash(plan))
        require(attempt and attempt.get('canonical_owned') and attempt.get('plan') == plan,'native_attempt_missing')
        console = route.connectsafely_app.direct
        observed = console._result(console._reconcile(plan,attempt['attempted_at']))
        require(observed.get('status') == 'sent' and observed.get('receipt_ref') == receipt,'native_readback_mismatch')
        return {'target':step['recipient'],'content':plan['content'],'effect_id':step['id']}


class NativeCadenceExecutor:
    def __init__(self, repository, ledger, *, routes, native_readers, clock=time.time, native_calendar=None):
        self.repo, self.ledger, self.clock = repository, ledger, clock
        readers = validate_readers(native_readers)
        require(bool(readers), 'native_readers_unavailable')
        require(isinstance(routes, dict) and bool(routes) and set(routes) <= {'ygor','loa'}
                and all(isinstance(route, NativeRoute) for route in routes.values()), 'native_routes_invalid')
        self.routes = MappingProxyType(dict(routes))
        self.native_readers = MappingProxyType(readers)
        self.native_calendar = deepcopy(DEFAULT_CALENDAR if native_calendar is None else native_calendar)
        self._prepared = {}

    def _scope(self, job, context=None):
        current = self.repo.effect(job['id'])
        require(current and current['payload'] == job['payload'] and current['effect_key'] == job['effect_key'], 'native_job_changed')
        cadence = self.repo.get_state('cadence', str(job['payload'].get('cadence_id','')))
        require(bool(cadence), 'native_cadence_missing')
        root = validate_native_context(cadence.get('native_context'))
        require(job['payload'].get('native_context') == root,'native_context_changed')
        require(context is None or context == root, 'native_context_changed')
        step = job['payload']['step']
        validate_native_step(step.get('native'), root)
        original = next((value for value in cadence.get('steps',[]) if value.get('id') == step.get('id')), None)
        fields = {'id','recipient','channel','content','order','interval_hours','subject','native'}
        require(original and _step_hash({key:value for key,value in original.items() if key in fields}) == job['effect_key']
                and _step_hash({key:value for key,value in step.items() if key in fields}) == job['effect_key'], 'native_step_changed')
        require(root['route'] in self.routes, 'native_route_unconfigured')
        return cadence, root, step, self.routes[root['route']]

    def binding_for(self, job, context):
        _, _, step, route = self._scope(job, context)
        if step['native']['action'] == 'email':
            adapter = route.gmail_adapter
            require(isinstance(adapter, GmailCadenceAdapter) and adapter.repo is self.repo
                    and adapter.enabled is True, 'native_email_unconfigured')
            return {'provider':'gmail','account_id':adapter.account_id}
        app = route.connectsafely_app
        require(app is not None and app.repo is self.repo and app.direct is not None, 'native_linkedin_unconfigured')
        return {'provider':'connectsafely','account_id':app.direct.account_id}

    def reader_for(self, job, context):
        self._scope(job,context)
        return NativeEffectReader(self,job,context)

    def _plan(self, job, context, step, route):
        require(callable(route.plan_loader), 'native_plan_reader_unavailable')
        plan = route.plan_loader(deepcopy(job), deepcopy(context))
        console = route.connectsafely_app.direct
        plan = console._plan(plan)
        require(plan['account_id'] == step['native']['sender_account']
                and plan['action'] == step['native']['action'] and plan['content'] == step['content']
                and ('https://www.linkedin.com/in/'+plan['recipient_public_id']).rstrip('/') == step['recipient'].rstrip('/'),
                'native_plan_mismatch')
        return plan

    def _acceptance(self, job, context, step, route, plan):
        if not step['native']['requires_connection']:
            return
        reader = route.relationship_reader
        require(callable(reader), 'native_acceptance_reader_unavailable')
        observed = reader(deepcopy(plan))
        require(isinstance(observed, dict) and observed.get('account_id') == plan['account_id']
                and observed.get('recipient_profile_urn') == plan['recipient_profile_urn']
                and observed.get('recipient_public_id') == plan['recipient_public_id']
                and observed.get('relationship_status') == 'CONNECTED'
                and type(observed.get('observed_at')) in (int,float)
                and 0 <= self.clock()-observed['observed_at'] <= 60, 'native_connection_required')

    def _parent(self, job, step, route):
        parent_id = step['native']['thread_parent_step_id']
        if parent_id is None:
            return None
        require(route.gmail_adapter.client.status().get('capabilities',{}).get('exact_reply_parent') is True,
                'exact_parent_unsupported')
        parents = [row for row in self.repo.effects() if row['payload'].get('cadence_id') == job['payload']['cadence_id']
                   and row['payload'].get('step',{}).get('id') == parent_id]
        require(len(parents) == 1 and parents[0]['state'] == 'sent' and parents[0].get('receipt_ref'), 'native_parent_unconfirmed')
        parent = parents[0]; previous = parent['payload']['step']
        require(previous.get('channel') == 'email' and previous.get('recipient') == step['recipient']
                and previous.get('subject') == step['subject']
                and previous.get('native',{}).get('sender_account') == step['native']['sender_account'], 'native_parent_mismatch')
        observed = self.reader_for(job, self.repo.get_state('cadence',job['payload']['cadence_id'])['native_context']).read_message(
            parent['receipt_ref'], reply_metadata=True)
        require(observed.get('from') == step['native']['sender_account'] and observed.get('to') == step['recipient']
                and observed.get('subject') == previous['subject'] and observed.get('content') == previous['content'],
                'native_parent_readback_mismatch')
        return observed

    def _policy(self, job, cadence):
        approval = self.repo.get_state('approval', cadence['id'])
        require(approval and approval.get('status') == 'active'
                and approval.get('step_hashes',{}).get(job['payload']['step']['id']) == job['effect_key'], 'native_approval_changed')
        step = job['payload']['step']; now = self.clock()
        expires = min(now+250, datetime.fromisoformat(approval['expires_at'].replace('Z','+00:00')).timestamp())
        return {'version':1, 'approval_ref':payload_hash(approval), 'mailbox':MAILBOX,
                'campaign_id':cadence['execution_context']['campaign_id'], 'cadence_id':cadence['id'],
                'step_id':step['id'], 'effect_key':job['effect_key'], 'recipient':step['recipient'],
                'subject':step['subject'], 'content_hash':payload_hash(step['content']),
                'issued_at':now, 'expires_at':expires, 'max_attempts':1}

    @staticmethod
    def _email_touch_step(job, step):
        # Existing email ledger scopes its target by subject. Native follow-ups
        # share a literal email subject, so pass the stable cadence-stage target
        # only to the ledger; the provider always receives the approved subject.
        stage = 'E'+str((step['order']+1)//2)
        return {**step, 'subject':'cadence:'+job['payload']['campaign_id']+':'+stage}

    def prepare_job(self, job):
        require(not self.repo.dispatch_fence_active, 'native_prepare_inside_fence')
        cadence, context, step, route = self._scope(job)
        require(self.binding_for(job,context)['account_id'] == step['native']['sender_account'], 'native_sender_mismatch')
        if step['native']['action'] != 'email':
            plan = self._plan(job,context,step,route)
            self._acceptance(job,context,step,route,plan)
            row = route.connectsafely_app.prepare_native(job,plan)
            self._prepared[job['id']] = {'kind':'linkedin','plan':plan,'cs_job':row}
            return
        parent = self._parent(job,step,route)
        policy = self._policy(job,cadence)
        key = GmailCadenceAdapter._key(policy)
        existing = self.repo.get_state('gmail_cadence_attempt',key)
        if existing:
            require(existing.get('job_id') == job['id'], 'native_email_attempt_mismatch')
            self._prepared[job['id']] = {'kind':'reconcile'}
            return
        def policy_loader():
            current = self.repo.get_state('approval',cadence['id'])
            require(current and payload_hash(current) == policy['approval_ref'], 'native_approval_changed')
            return deepcopy(policy)
        adapter = GmailCadenceAdapter(self.repo,client=route.gmail_adapter.client,enabled=True,
            policy_loader=policy_loader,clock=self.clock,native_parent=parent)
        adapter.prepare_job(job)
        # This ledger transaction commits independently before the workflow fence.
        created, touch = self.ledger.reserve(job['effect_key'],step['id'],self._email_touch_step(job,step))
        self._prepared[job['id']] = {'kind':'email' if created else 'reconcile', 'adapter':adapter, 'parent':parent}

    def preparation_absent(self, job):
        """Prove no durable preparation before deferring a temporal wait.

        Absence in the workflow DB alone cannot establish this after a crash:
        the independently committed canonical reservation is checked too.
        Any unavailable/conflicting read means retained, never safe to retry.
        """
        try:
            _, context, step, route = self._scope(job)
            if job['id'] in self._prepared:
                return False
            if step['native']['action'] == 'email':
                key = payload_hash({'mailbox':MAILBOX,'cadence':job['payload']['cadence_id'],'step':step['id']})
                if self.repo.get_state('gmail_cadence_attempt',key):
                    return False
                canonical = self._email_touch_step(job,step)
                evidence = self.ledger.preflight(job['effect_key'],canonical,raise_blocked=True)
                return (evidence.get('duplicate') is None
                        and self.ledger.reservation(job['effect_key'],canonical) is None)
            plan = self._plan(job,context,step,route)
            key = payload_hash({'native_job':job['id'],'effect_key':job['effect_key']})
            if (self.repo.get_state('connectsafely_job',key)
                    or self.repo.get_state('connectsafely_attempt',payload_hash(plan))):
                return False
            ledger = route.connectsafely_app.direct.ledger
            console = route.connectsafely_app.direct
            canonical = console._canonical_step(console._step(plan,'absence-check',1),plan)
            evidence = ledger.preflight(payload_hash(plan),canonical,raise_blocked=True)
            if evidence.get('duplicate') is not None:
                return False
            channel, action, person, target = ledger._scope(canonical,payload_hash(plan))
            with ledger._banco() as (connection, marker):
                cursor = connection.cursor()
                cursor.execute('SELECT alvo FROM toques WHERE canal='+marker+' AND acao='+marker
                               +' AND pessoa_chave='+marker+" AND status<>'liberado'",
                               (channel,action,person))
                if any(row[0]==target or (action=='mensagem' and not row[0].startswith('thunder-os#dm:'))
                       for row in cursor.fetchall()):
                    return False
                cursor.execute('SELECT touch_id FROM thunder_effect_touches WHERE effect_key='+marker,
                               (payload_hash(plan),))
                return cursor.fetchone() is None
        except Exception:
            return False

    @staticmethod
    def _cs_result(job, result):
        confirmed = result.get('status') == 'sent' and bool(result.get('receipt_ref'))
        return ProviderResult(job['payload']['step']['id'], 'confirmed' if confirmed else 'uncertain',
                              result.get('receipt_ref') if confirmed else None, result.get('error_code'))

    def execute_job(self, job, before_dispatch):
        require(getattr(self.repo._local,'dispatch_fence',None) == (job['id'],job['lease_owner']), 'native_dispatch_fence_required')
        prepared = self._prepared.get(job['id'])
        require(bool(prepared), 'native_job_not_prepared')
        if prepared['kind'] == 'reconcile':
            return self.reconcile_job(job)
        cadence, context, step, route = self._scope(job)
        if prepared['kind'] == 'linkedin':
            plan = self._plan(job,context,step,route)
            require(plan == prepared['plan'], 'native_plan_changed')
            def guard():
                self._acceptance(job,context,step,route,plan)
                return before_dispatch()
            result = route.connectsafely_app.execute_native(job,before_dispatch=guard)
            # Provider send acceptance alone never marks the outer job sent.
            if result.get('status') == 'sent':
                console = route.connectsafely_app.direct
                attempt = self.repo.get_state('connectsafely_attempt',payload_hash(plan))
                observed = console._result(console._reconcile(plan,attempt['attempted_at']))
                if observed.get('status') != 'sent' or observed.get('receipt_ref') != result.get('receipt_ref'):
                    result = {'status':'uncertain','error_code':'native_linkedin_readback_mismatch'}
                else:
                    result = observed
            elif result.get('status') == 'uncertain':
                result = route.connectsafely_app.reconcile_native(job)
            return self._cs_result(job,result)
        adapter = prepared['adapter']
        def guard():
            self.ledger.preflight(job['effect_key'],self._email_touch_step(job,step),raise_blocked=True)
            if prepared['parent'] is not None:
                require(self._parent(job,step,route) == prepared['parent'], 'native_parent_changed')
            return before_dispatch()
        effect = {'target':step['recipient'],'subject':step['subject'],'content':step['content'],'effect_id':step['id']}
        result = adapter.send(effect,before_dispatch=guard)
        if result.status == 'confirmed':
            try:
                self._email_readback(job,result.receipt_ref,prepared['parent'])
            except Exception:
                result = ProviderResult(step['id'],'uncertain',error_code='native_email_readback_mismatch')
        stored = self.ledger.finish(job['effect_key'], 'sent' if result.status == 'confirmed' else 'uncertain', result.receipt_ref,result.error_code)
        return ProviderResult(step['id'], 'confirmed' if stored['status']=='sent' else 'uncertain',stored.get('receipt_ref'),stored.get('error_code'))

    def _email_readback(self, job, receipt, parent=None):
        _, context, step, _ = self._scope(job)
        observed = self.reader_for(job,context).read_message(receipt,reply_metadata=parent is not None)
        require(observed['mailbox'] == step['native']['sender_account'] and observed['to'] == step['recipient']
                and observed['subject'] == step['subject'] and observed['content'] == step['content'], 'native_email_readback_mismatch')
        if parent is not None:
            references = [value for value in parent.get('source_references','').strip().split(' ') if value]
            require(len(references) <= 50 and all(re.fullmatch(r'<[^<>\s@]{1,250}@[^<>\s@]{1,250}>',value) for value in references), 'native_parent_references_invalid')
            if not references or references[-1] != parent['source_message_id']:
                references.append(parent['source_message_id'])
            require(observed['source_thread_id'] == parent['source_thread_id']
                    and observed['source_in_reply_to'] == parent['source_message_id']
                    and observed['source_references'] == ' '.join(references), 'native_email_parent_mismatch')
        return observed

    def technical_policy_for(self, job):
        """Exact nine-field bridge policy, plus paired anchors for a follow-up.

        This constructor-trusted loader projects the SAME native approval. It is
        never installed in an environment/file or accepted from request content.
        """
        cadence, _, step, _ = self._scope(job)
        require(step['native']['action'] == 'email','native_policy_channel_mismatch')
        key = payload_hash({'mailbox':MAILBOX,'cadence':cadence['id'],'step':step['id']})
        row = self.repo.get_state('gmail_cadence_attempt',key)
        approval = self.repo.get_state('approval',cadence['id'])
        require(row and approval and approval.get('status') == 'active'
                and row['job_id'] == job['id'] and row['policy']['approval_ref'] == payload_hash(approval), 'native_approval_changed')
        envelope, policy = row['envelope'], row['policy']
        require(policy['effect_key'] == job['effect_key'] and envelope['to'] == step['recipient']
                and envelope['subject'] == step['subject'] and envelope['body'] == step['content'], 'native_policy_scope_mismatch')
        expires = datetime.fromisoformat(envelope['expires_at'].replace('Z','+00:00')).timestamp()
        result = {'approval_ref':policy['approval_ref'],'mailbox':MAILBOX,'to':envelope['to'],
            'subject':envelope['subject'],'body_hash':hashlib.sha256(envelope['body'].encode()).hexdigest(),
            'idempotency_key':envelope['idempotency_key'],'issued_at':int(policy['issued_at']*1000),
            'expires_at':int(min(policy['expires_at'],expires)*1000),'max_attempts':1}
        if 'reply_message_id' in envelope:
            result.update(thread_id=envelope['thread_id'],reply_message_id=envelope['reply_message_id'])
        require((step['native']['thread_parent_step_id'] is not None) == ('reply_message_id' in result), 'native_policy_parent_mismatch')
        return result

    def reconcile_job(self, job):
        """Observe an existing attempt; never send, release or requeue anything."""
        _, context, step, route = self._scope(job)
        if step['native']['action'] != 'email':
            try:
                return self._cs_result(job,route.connectsafely_app.reconcile_native(job))
            except Exception:
                return ProviderResult(step['id'],'uncertain',error_code='native_reconciliation_required')
        key = payload_hash({'mailbox':MAILBOX,'cadence':job['payload']['cadence_id'],'step':step['id']})
        row = self.repo.get_state('gmail_cadence_attempt',key)
        # A missing workflow marker with a durable reservation is NOT zero sends.
        if not row or row.get('phase') not in {'dispatch_started','uncertain','sent'}:
            return ProviderResult(step['id'],'uncertain',error_code='native_reconciliation_required')
        try:
            result = route.gmail_adapter.client.reconcile(row['envelope']['idempotency_key'])
            receipt = 'gmail:'+result['provider_id']
            require(result.get('mailbox') == MAILBOX and result.get('status') == 'sent','native_reconciliation_required')
            parent = self._parent(job,step,route)
            self._email_readback(job,receipt,parent)
            stored = self.ledger.finish(job['effect_key'],'sent',receipt)
            return ProviderResult(step['id'],'confirmed' if stored['status']=='sent' else 'uncertain',stored.get('receipt_ref'))
        except Exception:
            return ProviderResult(step['id'],'uncertain',error_code='native_reconciliation_required')
