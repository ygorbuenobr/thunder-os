"""One authenticated application facade and durable worker queue.

Composition is explicit. No environment, credentials, migrations, network probes
or provider selection happen here. Simulations are always labelled as such.
"""
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from .connectsafely_console import ConnectSafelyError, SAFE_CODES
from .db import RepositoryConflict
from .models import payload_hash
from .approvals import ApprovalError
from .delegated_suppression import ContactScope, SuppressionProof


class ConnectSafelyApplication:
    def __init__(self, repository, *, direct_console=None, social_console=None,
                 provider=None, simulation=False, execution_enabled=False,
                 readiness=None, action_readiness=None, execution_scope=None, clock=time.time, lease_seconds=120,
                 delegated_suppression=None, delegated_contact_scope=None):
        if type(simulation) is not bool or type(execution_enabled) is not bool:
            raise ValueError('explicit application modes required')
        if type(lease_seconds) is not int or not 15 <= lease_seconds <= 300:
            raise ValueError('bounded worker lease required')
        self.repo = repository
        self.direct = direct_console
        self.social = social_console
        self.provider = provider
        self.simulation = simulation
        self.execution_enabled = execution_enabled
        if execution_scope is not None and (not isinstance(execution_scope,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",execution_scope)):
            raise ValueError("invalid_execution_scope")
        self.execution_scope = execution_scope
        self.action_readiness = action_readiness
        self.readiness = readiness
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.delegated_task_guard = None
        self.delegated_suppression = delegated_suppression
        self.delegated_contact_scope = delegated_contact_scope
        for console in (self.direct, self.social):
            if console is not None and console.repo is not self.repo:
                raise ValueError('application and consoles must share repository')

    @staticmethod
    def _actor(actor):
        if actor != 'ygor':
            raise ConnectSafelyError('human_authority_required')

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
            raise ConnectSafelyError('invalid_preview_id')
        return value

    def _console(self, action):
        console = self.direct if action in ('dm', 'invite') else self.social if action in ('comment', 'reply', 'react_post') else None
        if console is None:
            raise ConnectSafelyError('adapter_not_configured')
        return console

    def _configured(self):
        return self.direct is not None or self.social is not None

    def _simulation_boundary(self):
        if self.simulation:
            from .connectsafely_composition import _assert_simulated
            try:
                _assert_simulated(self.provider)
                if any(console is not None and console.client is not self.provider
                       for console in (self.direct, self.social)):
                    raise ValueError('different_console_port')
            except (ValueError, AttributeError):
                raise ConnectSafelyError('simulation_transport_unverified') from None

    def _blockers(self, action=None):
        blockers = []
        try:
            self._simulation_boundary()
        except ConnectSafelyError:
            blockers.append({'code':'simulation_transport_unverified', 'action':'Usar exclusivamente portas sintéticas verificadas para a simulação.'})
        if not self._configured() or (action in ("dm","invite") and self.direct is None) or (action in ("comment","reply","react_post") and self.social is None):
            blockers.append({'code':'adapter_not_configured', 'action':'Configurar uma composição explícita e revisada do provider.'})
        if self.execution_enabled is not True:
            blockers.append({'code':'external_effects_disabled', 'action':'Manter efeitos desligados até validar os pré-requisitos e aprovar a ativação.'})
        if not self.simulation:
            try:
                observation = (self.action_readiness(action) if callable(self.action_readiness) and action is not None
                               else self.readiness() if callable(self.readiness) else None)
            except Exception:
                observation = None
            if not isinstance(observation, dict) or observation.get('ready') is not True:
                blockers.append({'code':'activation_evidence_unverified', 'action':'Verificar coordenação, identidade, autoria e quotas; configuração não comprova prontidão.'})
        if (self.repo.get_state('control','global') or {}).get('paused'):
            blockers.append({'code':'global_pause','action':'Retomar explicitamente a operação antes de aprovar novos efeitos.'})
        return blockers

    def status(self, actor):
        self._actor(actor)
        console = self.direct or self.social
        actions = {}
        for action in ('dm','invite','comment','reply','react_post'):
            action_blockers=self._blockers(action)
            actions[action]={'enqueue_enabled':not action_blockers,'blockers':action_blockers}
        blockers = [] if any(a['enqueue_enabled'] for a in actions.values()) else self._blockers()
        if not blockers and not any(a['enqueue_enabled'] for a in actions.values()):
            blockers = [{'code':'activation_evidence_unverified','action':'Nenhuma ação pronta.'}]
        return {'action_capabilities':actions,'individual_execution':self.execution_scope is not None,'configured':self._configured(), 'simulation':self.simulation,
                'mode':'simulation' if self.simulation else 'blocked' if blockers else 'gated',
                'enqueue_enabled':not blockers, 'account_id':console.account_id if console else None,
                'account_public_id':console.account_public_id if console else None,
                'blockers':blockers,
                'capabilities':{'read':self.provider is not None, 'prepare':self._configured(),
                                'enqueue':not blockers, 'reconcile':self._configured()}}

    def _enabled(self, action=None):
        blockers = self._blockers(action)
        if blockers:
            raise ConnectSafelyError(blockers[0]['code'])
        if (self.repo.get_state('control', 'global') or {}).get('paused'):
            raise ConnectSafelyError('global_pause')

    def prepare(self, body, actor):
        self._actor(actor)
        self._simulation_boundary()
        if not isinstance(body, dict):
            raise ConnectSafelyError('invalid_plan')
        preview = self._console(body.get('action')).prepare(body, actor=actor)
        self.repo.append_state('connectsafely_preview_origin', preview['id'], {
            'simulation':self.simulation, 'review_hash':preview['review_hash'],
            'account_id':preview['plan']['account_id'], 'execution_scope':self.execution_scope, 'created_at':self.clock()})
        return {**preview, 'simulation':self.simulation, 'job':None}

    def prepare_delegated(self, body, authority):
        """Prepare an exact D0-approved cadence effect without impersonating Ygor."""
        from .hermes_linkedin_cadence import validate_delegated_plan
        validate_delegated_plan(self.repo, authority, body, self.clock)
        self._simulation_boundary()
        preview = self._console(body.get('action')).prepare(body, actor='ygor')
        self.repo.append_state('connectsafely_preview_origin', preview['id'], {
            'simulation':self.simulation, 'review_hash':preview['review_hash'],
            'account_id':preview['plan']['account_id'], 'execution_scope':self.execution_scope,
            'created_at':self.clock(), 'executor':'hermes (delegado)',
            'delegated_authority':dict(authority)})
        return {**preview, 'simulation':self.simulation, 'job':None}

    def _preview(self, ident):
        preview = self.repo.get_state('connectsafely_preview', self._id(ident))
        if preview is None:
            raise ConnectSafelyError('preview_missing')
        return preview

    def _origin_mode(self,preview):
        origin = self.repo.get_state('connectsafely_preview_origin',preview['id'])
        if (not origin or type(origin.get('simulation')) is not bool
                or origin.get('review_hash') != preview['review_hash']
                or origin.get('account_id') != preview['plan']['account_id']):
            return None
        return origin['simulation']

    def _check_mode(self,preview):
        if self._origin_mode(preview) is not self.simulation:
            raise ConnectSafelyError('preview_mode_mismatch')
        if self.execution_scope is not None:
            origin=self.repo.get_state('connectsafely_preview_origin',preview['id'])
            if not origin or origin.get('execution_scope') != self.execution_scope:
                raise ConnectSafelyError('preview_activation_mismatch')

    def preview(self, ident, actor):
        self._actor(actor)
        preview = self._preview(ident)
        console = self._console(preview['plan']['action'])
        value = console.read_preview(ident, actor=actor)
        jobs = [j for j in self.repo.list_state('connectsafely_job') if j['preview_id'] == ident]
        return {**value, 'simulation':self._origin_mode(preview), 'job':self._public_job(jobs[-1]) if jobs else None}

    def listpreviews(self, actor):
        self._actor(actor)
        return {'previews':[self.preview(p['id'], actor) for p in self.repo.list_state('connectsafely_preview')[-50:]],
                'simulation':self.simulation}

    @staticmethod
    def _public_job(job):
        return {key:job[key] for key in ('id','preview_id','state','simulation','result','error_code') if key in job}

    def campaign_review(self, body, actor):
        self._actor(actor)
        if not isinstance(body, dict) or set(body) != {'preview_id','campaign_id'}:
            raise ConnectSafelyError('campaign_review_invalid')
        preview = self._preview(body['preview_id'])
        return self._console(preview['plan']['action']).campaign_review(body, actor)

    def approve_delegated_campaign(self, body, actor):
        """Human-facing approval entry; callers must authenticate the actor."""
        self._actor(actor)
        if (not isinstance(body, dict) or set(body) != {'preview_id', 'expected_version', 'review_hash', 'execution_confirmation'}
                or type(body.get('expected_version')) is not int):
            raise ConnectSafelyError('exact_review_required')
        with self.repo.transaction():
            preview = self._preview(body['preview_id'])
            origin = self.repo.get_state('connectsafely_preview_origin', preview['id'])
            if not origin or origin.get('executor') != 'hermes (delegado)':
                raise ConnectSafelyError('delegated_authority_changed')
            self._check_mode(preview)
            if preview['version'] != body['expected_version'] or preview['review_hash'] != body['review_hash']:
                raise RepositoryConflict('preview_changed')
            try:
                record = self._console(preview['plan']['action']).campaign_gate().approve(body, actor)
            except ApprovalError as exc:
                raise ConnectSafelyError(str(exc)) from None
            return {'campaign_approval_id': record['id'], 'preview_id': preview['id']}

    def enqueue(self, body, actor):
        self._actor(actor)
        if not isinstance(body, dict) or 'execution_confirmation' not in body:
            raise ConnectSafelyError('campaign_confirmation_required')
        if (not isinstance(body, dict) or set(body) != {'preview_id','expected_version','review_hash','execution_confirmation'}
                or type(body.get('expected_version')) is not int):
            raise ConnectSafelyError('exact_review_required')
        with self.repo.transaction():
            preview = self._preview(body['preview_id'])
            gate = self._console(preview['plan']['action']).campaign_gate()
            try: campaign_record = gate.approve(body, actor)
            except ApprovalError as exc: raise ConnectSafelyError(str(exc)) from None
            self._enabled(preview['plan']['action'])
            self._check_mode(preview)
            if preview['version'] != body['expected_version'] or preview['review_hash'] != body['review_hash']:
                raise RepositoryConflict('preview_changed')
            if preview['state'] != 'review_required' or preview['expires_at'] <= self.clock():
                raise ConnectSafelyError('preview_expired_or_consumed')
            expected = payload_hash({'plan':preview['plan'], 'version':preview['version']})
            if expected != body['review_hash']:
                raise RepositoryConflict('preview_changed')
            self._console(preview['plan']['action'])
            key = payload_hash({'review':body, 'simulation':self.simulation})
            existing = self.repo.get_state('connectsafely_job', key)
            if existing is None:
                self.repo.append_state('connectsafely_queue_approval', key, {
                    'review':dict(body), 'actor':'ygor', 'simulation':self.simulation,
                    'campaign_confirmation':campaign_record,
                    'expires_at':preview['expires_at'], 'execution_scope':self.execution_scope, 'approved_at':self.clock()})
                existing = self.repo.append_state('connectsafely_job', key, {
                    'preview_id':preview['id'], 'review':dict(body), 'actor':'ygor',
                    'campaign_confirmation_hash':payload_hash(campaign_record),
                    'state':'queued', 'simulation':self.simulation, 'execution_scope':self.execution_scope, 'created_at':self.clock()})
            return {'job_id':key, 'preview_id':preview['id'], 'state':existing['state'], 'simulation':self.simulation}

    def prepare_native(self, job, plan):
        """Internal canonical-job bridge; no enqueue/queue approval is manufactured."""
        self._enabled(plan.get('action'))
        console = self._console(plan.get('action'))
        plan = console._plan(plan)
        console._identity()
        native = job['payload']['step'].get('native', {})
        if (native.get('action') != plan['action'] or native.get('sender_account') != plan['account_id']
                or job['payload']['step']['content'] != plan['content']):
            raise ConnectSafelyError('native_plan_mismatch')
        key = payload_hash({'native_job':job['id'], 'effect_key':job['effect_key']})
        existing = self.repo.get_state('connectsafely_job', key)
        if existing:
            preview = self._preview(existing['preview_id'])
            if (existing.get('native_job_id') != job['id'] or preview['plan'] != plan
                    or existing.get('native_payload_hash') != payload_hash(job['payload'])):
                raise ConnectSafelyError('native_plan_mismatch')
            return existing
        preview = console.prepare(plan, actor='ygor')
        review = {'preview_id':preview['id'], 'expected_version':preview['version'], 'review_hash':preview['review_hash']}
        return self.repo.append_state('connectsafely_job', key, {
            'native_job_id':job['id'], 'native_effect_key':job['effect_key'],
            'native_payload_hash':payload_hash(job['payload']), 'preview_id':preview['id'],
            'review':review, 'state':'queued', 'simulation':self.simulation, 'created_at':self.clock()})

    def execute_native(self, job, *, before_dispatch):
        key = payload_hash({'native_job':job['id'], 'effect_key':job['effect_key']})
        row = self.repo.get_state('connectsafely_job', key)
        if (not row or row.get('native_payload_hash') != payload_hash(job['payload'])
                or not callable(before_dispatch)):
            raise ConnectSafelyError('native_job_mismatch')
        preview = self._preview(row['preview_id'])
        self._enabled(preview['plan']['action'])
        if row['state'] != 'queued':
            return row.get('result') or {'status':'uncertain','error_code':'requires_reconciliation','retry_write':False}
        def guard():
            self._enabled(preview['plan']['action'])
            return before_dispatch()
        result = self._console(preview['plan']['action']).execute_native(row['review'], job, before_dispatch=guard)
        state = {'sent':'done','blocked':'blocked'}.get(result.get('status'), 'needs_reconciliation')
        self.repo.put_state('connectsafely_job', key, {**row, 'state':state, 'result':result})
        return result

    def reconcile_native(self, job):
        key = payload_hash({'native_job':job['id'], 'effect_key':job['effect_key']})
        row = self.repo.get_state('connectsafely_job', key)
        if not row or row.get('native_payload_hash') != payload_hash(job['payload']):
            return {'status':'uncertain','error_code':'requires_reconciliation','retry_write':False}
        preview = self._preview(row['preview_id'])
        # A previously confirmed receipt still needs fresh provider readback when
        # the outer workflow lost its local result. Never trust a UI/job label.
        console = self._console(preview['plan']['action'])
        attempt = self.repo.get_state('connectsafely_attempt',payload_hash(preview['plan']))
        if attempt and attempt.get('status') == 'sent':
            observed = console._result(console._reconcile(preview['plan'],attempt['attempted_at']))
            if observed.get('status') == 'sent' and observed.get('receipt_ref') == attempt.get('receipt_ref'):
                return observed
            return {'status':'uncertain','error_code':'receipt_unverified','retry_write':False}
        # Read-only reconciliation reuses the owned CS attempt, never execute/enqueue.
        return console.reconcile({'preview_id':preview['id']}, actor='ygor')
    def enqueue_delegated(self, body, authority):
        """Queue only a preview bound to the same current D0 authority."""
        from .hermes_linkedin_cadence import validate_delegated_plan
        if not isinstance(body, dict) or not body.get('campaign_approval_id'):
            raise ConnectSafelyError('campaign_confirmation_required')
        if (set(body) != {'preview_id','expected_version','review_hash','campaign_approval_id'}
                or not isinstance(body.get('campaign_approval_id'), str)
                or type(body.get('expected_version')) is not int):
            raise ConnectSafelyError('exact_review_required')
        with self.repo.transaction():
            preview = self._preview(body['preview_id'])
            self._enabled(preview['plan']['action'])
            self._check_mode(preview)
            origin = self.repo.get_state('connectsafely_preview_origin', preview['id'])
            if (not origin or origin.get('executor') != 'hermes (delegado)'
                    or origin.get('delegated_authority') != dict(authority)):
                raise ConnectSafelyError('delegated_authority_changed')
            current = validate_delegated_plan(self.repo, authority, preview['plan'], self.clock)
            if preview['version'] != body['expected_version'] or preview['review_hash'] != body['review_hash']:
                raise RepositoryConflict('preview_changed')
            if preview['state'] != 'review_required' or preview['expires_at'] <= self.clock():
                raise ConnectSafelyError('preview_expired_or_consumed')
            expected = payload_hash({'plan':preview['plan'], 'version':preview['version']})
            if expected != body['review_hash']:
                raise RepositoryConflict('preview_changed')
            campaign_record = self.repo.get_state('operation_approval', body['campaign_approval_id'])
            if not campaign_record or campaign_record.get('preview_id') != preview['id'] or campaign_record.get('preview_version') != preview['version']:
                raise ConnectSafelyError('campaign_confirmation_required')
            try:
                self._console(preview['plan']['action']).campaign_gate().validate(campaign_record)
            except ApprovalError as exc:
                raise ConnectSafelyError(str(exc)) from None
            review = {'preview_id': body['preview_id'], 'expected_version': body['expected_version'],
                      'review_hash': body['review_hash'],
                      'execution_confirmation': campaign_record['execution_confirmation']['request']}
            key = payload_hash({'review':review, 'simulation':self.simulation,
                                'authority_id':authority['id'], 'authority_version':authority['version']})
            existing = self.repo.get_state('connectsafely_job', key)
            if existing is None:
                day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
                self.repo.reserve_delegated_action('hermes', 'linkedin_cadence', day)
                approval = {
                    'review':review, 'actor':'ygor', 'executor':'hermes (delegado)',
                    'campaign_confirmation':campaign_record,
                    'simulation':self.simulation, 'expires_at':preview['expires_at'],
                    'execution_scope':self.execution_scope, 'approved_at':self.clock(),
                    'delegated_authority':dict(authority),
                    'delegation_version':self.repo.get_state('delegation','hermes')['version'],
                    'cadence_approval_hash':current['review_hash'],
                }
                self.repo.append_state('connectsafely_queue_approval', key, approval)
                existing = self.repo.append_state('connectsafely_job', key, {
                    'preview_id':preview['id'], 'review':review, 'actor':'hermes (delegado)',
                    'campaign_confirmation_hash':payload_hash(campaign_record),
                    'state':'queued', 'simulation':self.simulation,
                    'execution_scope':self.execution_scope, 'created_at':self.clock(),
                    'delegated_authority':dict(authority)})
            return {'job_id':key, 'preview_id':preview['id'], 'state':existing['state'], 'simulation':self.simulation}

    def _job_authorized(self,job):
        self._check_mode(self._preview(job['preview_id']))
        approval = self.repo.get_state('connectsafely_queue_approval', job['id'])
        record = (approval or {}).get('campaign_confirmation')
        if not record or job.get('campaign_confirmation_hash') != payload_hash(record):
            raise ConnectSafelyError('campaign_confirmation_required')
        preview = self._preview(job['preview_id'])
        try: self._console(preview['plan']['action']).campaign_gate().validate(record)
        except ApprovalError as exc: raise ConnectSafelyError(str(exc)) from None
        if (not approval or approval.get('actor') != 'ygor'
                or approval.get('review') != job.get('review')
                or approval.get('simulation') is not self.simulation
                or approval.get('expires_at',0) <= self.clock()
                or (self.execution_scope is not None and (job.get('execution_scope') != self.execution_scope
                    or approval.get('execution_scope') != self.execution_scope))):
            raise ConnectSafelyError('job_authority_invalid')
        if job.get('actor') == 'hermes (delegado)':
            from .hermes_linkedin_cadence import validate_delegated_plan
            authority = job.get('delegated_authority')
            if (approval.get('executor') != 'hermes (delegado)'
                    or approval.get('delegated_authority') != authority):
                raise ConnectSafelyError('job_authority_invalid')
            delegation = self.repo.get_state('delegation','hermes')
            if not delegation or approval.get('delegation_version') != delegation.get('version'):
                raise ConnectSafelyError('delegated_authority_changed')
            preview = self._preview(job['preview_id'])
            current = validate_delegated_plan(self.repo, authority, preview['plan'], self.clock)
            if approval.get('cadence_approval_hash') != current.get('review_hash'):
                raise ConnectSafelyError('delegated_authority_changed')
        elif job.get('actor') != 'ygor' or approval.get('executor') is not None:
            raise ConnectSafelyError('job_authority_invalid')

    def _delegated_reconciliation_authorized(self, job, preview):
        """Validate current D0 authority without reopening the dispatch lease."""
        from .hermes_linkedin_cadence import validate_delegated_plan
        approval=self.repo.get_state('connectsafely_queue_approval',job['id'])
        authority=job.get('delegated_authority')
        delegation=self.repo.get_state('delegation','hermes')
        if (job.get('actor')!='hermes (delegado)' or not approval
                or approval.get('actor')!='ygor' or approval.get('executor')!='hermes (delegado)'
                or approval.get('delegated_authority')!=authority
                or approval.get('review')!=job.get('review')
                or approval.get('simulation') is not self.simulation
                or job.get('simulation') is not self.simulation
                or job.get('execution_scope')!=self.execution_scope
                or approval.get('execution_scope')!=self.execution_scope
                or not delegation or approval.get('delegation_version')!=delegation.get('version')):
            raise ConnectSafelyError('job_authority_invalid')
        current=validate_delegated_plan(self.repo,authority,preview['plan'],self.clock)
        if approval.get('cadence_approval_hash')!=current.get('review_hash'):
            raise ConnectSafelyError('delegated_authority_changed')

    def _claim(self, job_id=None):
        if self.execution_scope is not None and job_id is None:
            return None
        with self.repo.transaction():
            jobs=([self.repo.get_state('connectsafely_job',self._id(job_id))] if job_id is not None
                  else self.repo.list_state('connectsafely_job'))
            for job in jobs:
                if job is not None and 'native_job_id' in job:
                    continue
                if job is None or (self.execution_scope is not None and job.get('execution_scope') != self.execution_scope):
                    continue
                if job['state'] == 'queued':
                    try: self._job_authorized(job)
                    except (ConnectSafelyError, ApprovalError):
                        self.repo.put_state('connectsafely_job', job['id'], {
                            **job, 'state':'needs_reconciliation', 'error_code':'campaign_confirmation_invalid'})
                        continue
                if job['state'] == 'running' and job['lease_until'] <= self.clock():
                    # Never redeliver a crashed worker's potentially dispatched action.
                    self.repo.put_state('connectsafely_job', job['id'], {
                        **job, 'state':'needs_reconciliation', 'error_code':'worker_lease_expired'})
                elif job['state'] == 'queued':
                    return self.repo.put_state('connectsafely_job', job['id'], {
                        **job, 'state':'running', 'claim_token':uuid4().hex,
                        'lease_until':self.clock()+self.lease_seconds})
        return None

    def process_one(self, *, job_id=None):
        if self.execution_scope is not None and job_id is None:
            return False
        try:
            selected=self.repo.get_state('connectsafely_job',self._id(job_id)) if job_id is not None else None
            if job_id is not None and selected is None:return False
            action=self._preview(selected['preview_id'])['plan']['action'] if selected else None
            job = self._claim(job_id)
            if job is not None: self._enabled(action)
        except ConnectSafelyError:
            return False
        if job is None:
            return False
        try:
            if job.get('actor') not in {'ygor','hermes (delegado)'} or job.get('simulation') is not self.simulation:
                raise ConnectSafelyError('job_authority_invalid')
            self._job_authorized(job)
            preview = self._preview(job['preview_id'])
            suppression = {}
            def dispatch_guard():
                self._enabled(preview['plan']['action'])
                self._job_authorized(job)
                if job.get('actor') == 'hermes (delegado)':
                    from .hermes_linkedin_cadence import validate_delegated_plan
                    current = validate_delegated_plan(self.repo, job['delegated_authority'], preview['plan'], self.clock)
                    scope, proof = suppression.get('scope'), suppression.get('proof')
                    if (not isinstance(scope, ContactScope) or not isinstance(proof, SuppressionProof)
                            or scope.hubspot_contact_id != current.get('hubspot_contact_id')
                            or scope.linkedin_profile_urn != preview['plan'].get('recipient_profile_urn')
                            or not proof.valid_for(scope)):
                        raise ConnectSafelyError('dispatch_guard_denied')
                with self.repo.transaction():
                    current = self.repo.get_state('connectsafely_job', job['id'])
                    if (not current or current['state'] != 'running'
                            or current.get('claim_token') != job['claim_token']
                            or current['lease_until'] <= self.clock()
                            or current['review'] != job['review']):
                        raise ConnectSafelyError('worker_lease_expired')
                    self.repo.put_state('connectsafely_job', job['id'], {**current, 'dispatch_started':True})
                return True
            def delegated_check():
                guard = self.delegated_task_guard
                if not callable(guard):
                    raise ConnectSafelyError('dispatch_guard_denied')
                try:
                    if guard(job) is not True:
                        raise ConnectSafelyError('dispatch_guard_denied')
                    if not callable(self.delegated_contact_scope) or not callable(getattr(self.delegated_suppression, 'check', None)):
                        raise ConnectSafelyError('dispatch_guard_denied')
                    scope = self.delegated_contact_scope(job, preview)
                    from .hermes_linkedin_cadence import validate_delegated_plan
                    current = validate_delegated_plan(self.repo, job['delegated_authority'], preview['plan'], self.clock)
                    if (not isinstance(scope, ContactScope)
                            or scope.hubspot_contact_id != current.get('hubspot_contact_id')
                            or scope.linkedin_profile_urn != preview['plan'].get('recipient_profile_urn')):
                        raise ConnectSafelyError('dispatch_guard_denied')
                    proof = self.delegated_suppression.check(scope)
                    if not isinstance(proof, SuppressionProof) or not proof.valid_for(scope):
                        raise ConnectSafelyError('dispatch_guard_denied')
                    suppression.update(scope=scope, proof=proof)
                except Exception:
                    raise ConnectSafelyError('dispatch_guard_denied') from None
                return True
            options = {'pre_dispatch_check': delegated_check} if job.get('actor') == 'hermes (delegado)' else {}
            result = self._console(preview['plan']['action']).execute(
                job['review'], actor='ygor', dispatch_guard=dispatch_guard, **options)
            state = {'sent':'done','blocked':'blocked','uncertain':'needs_reconciliation'}.get(result.get('status'),'needs_reconciliation')
        except RepositoryConflict:
            result = {'status':'blocked','error_code':'preview_changed','retry_write':False}
            state = 'blocked'
        except Exception as exc:
            code = str(exc) if isinstance(exc, ConnectSafelyError) and str(exc) in (SAFE_CODES | {'worker_lease_expired','job_authority_invalid','preview_expired_or_consumed','preview_mode_mismatch'}) else 'execution_blocked'
            result = {'status':'blocked','error_code':code,'retry_write':False}
            state = 'blocked'
        with self.repo.transaction():
            current = self.repo.get_state('connectsafely_job', job['id'])
            # A dispatch boundary followed by an exception cannot prove no effect.
            # Keep it fenced for explicit reconciliation, never automatic retry.
            if current and current.get('dispatch_started') and state == 'blocked':
                state = 'needs_reconciliation'
                result = {**result, 'status':'uncertain', 'retry_write':False}
            if (current and current['state'] == 'running' and current.get('claim_token') == job['claim_token']
                    and current['lease_until'] > self.clock()):
                self.repo.put_state('connectsafely_job', job['id'], {**current, 'state':state, 'result':result})
            elif current and current['state'] == 'running' and current.get('claim_token') == job['claim_token']:
                self.repo.put_state('connectsafely_job', job['id'], {**current, 'state':'needs_reconciliation', 'error_code':'worker_lease_expired'})
        return True

    def reconcile(self, body, actor):
        self._actor(actor)
        self._simulation_boundary()
        if not isinstance(body,dict) or set(body) != {'preview_id'}:
            raise ConnectSafelyError('invalid_reconciliation')
        preview = self._preview(body['preview_id'])
        self._check_mode(preview)
        if preview['plan']['action']=='invite':
            return self._reconcile_invitation_job(preview, body, actor)
        result = self._console(preview['plan']['action']).reconcile(body, actor=actor)
        return {**result, 'simulation':self.simulation}

    def reconcile_delegated(self, body, authority):
        """Reconcile a Hermes job without turning the executor into human authority."""
        self._simulation_boundary()
        if not isinstance(body,dict) or set(body) != {'preview_id'}:
            raise ConnectSafelyError('invalid_reconciliation')
        preview = self._preview(body['preview_id'])
        self._check_mode(preview)
        origin = self.repo.get_state('connectsafely_preview_origin', preview['id'])
        if (not origin or origin.get('executor') != 'hermes (delegado)'
                or origin.get('delegated_authority') != dict(authority)):
            raise ConnectSafelyError('delegated_authority_changed')
        jobs=[j for j in self.repo.list_state('connectsafely_job') if j['preview_id']==preview['id']]
        if len(jobs)!=1 or jobs[0].get('delegated_authority') != dict(authority):
            raise ConnectSafelyError('job_authority_invalid')
        self._delegated_reconciliation_authorized(jobs[0], preview)
        if preview['plan']['action']=='invite':
            return self._reconcile_invitation_job(preview, body, 'hermes (delegado)')
        result = self._console(preview['plan']['action']).reconcile(body, actor='ygor')
        return {**result, 'simulation':self.simulation}

    def _reconcile_invitation_job(self, preview, body, actor):
        from .approvals import _step_hash
        jobs=[j for j in self.repo.list_state('connectsafely_job') if j['preview_id']==preview['id']]
        if len(jobs)!=1:raise ConnectSafelyError('job_authority_invalid')
        job=jobs[0];review=job.get('review',{})
        approval=self.repo.get_state('connectsafely_queue_approval',job['id'])
        attempt=self.repo.get_state('connectsafely_attempt',preview.get('effect_key',''))
        console=self._console('invite')
        delegated=actor=='hermes (delegado)'
        if actor not in {'ygor','hermes (delegado)'}:
            raise ConnectSafelyError('job_authority_invalid')
        if (job.get('state') not in {'needs_reconciliation','done'} or not job.get('dispatch_started')
                or job.get('actor')!=('hermes (delegado)' if delegated else 'ygor')
                or job.get('simulation') is not self.simulation
                or not approval or approval.get('actor')!='ygor' or approval.get('review')!=review
                or approval.get('executor')!=('hermes (delegado)' if delegated else None)
                or approval.get('simulation') is not self.simulation
                or job.get('execution_scope')!=self.execution_scope or approval.get('execution_scope')!=self.execution_scope
                or not attempt or attempt.get('preview_id')!=preview['id']
                or attempt.get('attempt_token')!=preview.get('attempt_token')
                or attempt.get('attempt_token')!=job.get('result',{}).get('attempt_token')
                or attempt.get('plan')!=preview['plan'] or payload_hash(preview['plan'])!=preview.get('effect_key')
                or type(review.get('expected_version')) is not int
                or review.get('review_hash')!=preview['review_hash']
                or review['review_hash']!=payload_hash({'plan':preview['plan'],'version':review['expected_version']})
                or not approval.get('approved_at',float('inf'))<=attempt.get('attempted_at',0)<approval.get('expires_at',0)
                or attempt.get('approval_hash')!=_step_hash(console._step(preview['plan'],preview['id'],review['expected_version']))):
            raise ConnectSafelyError('job_authority_invalid')
        if delegated:
            self._delegated_reconciliation_authorized(job, preview)
        def reconciliation_guard():
            current=self.repo.get_state('connectsafely_job',job['id'])
            current_approval=self.repo.get_state('connectsafely_queue_approval',job['id'])
            if (not current or current['version']!=job['version'] or current.get('state')!=job['state']
                    or current.get('review')!=review or current_approval!=approval):
                raise RepositoryConflict('attempt_changed')
            return True
        result=console.reconcile(body,actor='ygor',reconciliation_guard=reconciliation_guard)
        with self.repo.transaction():
            current=self.repo.get_state('connectsafely_job',job['id'])
            current_attempt=self.repo.get_state('connectsafely_attempt',preview['effect_key'])
            if (not current or current['version']!=job['version'] or current.get('review')!=review
                    or current_attempt.get('attempt_token')!=attempt['attempt_token']
                    or result.get('attempt_token')!=attempt['attempt_token']):
                raise RepositoryConflict('attempt_changed')
            if current['state']=='done':return {**current['result'],'simulation':self.simulation}
            if result.get('status')=='sent':
                if (current_attempt.get('status')!='sent' or current_attempt.get('receipt_ref')!=result.get('receipt_ref')):
                    raise RepositoryConflict('attempt_changed')
                audit_id=payload_hash({'job_id':job['id'],'attempt_token':attempt['attempt_token']})
                self.repo.append_state('connectsafely_reconciliation',audit_id,{
                    'job_id':job['id'],'preview_id':preview['id'],'effect_key':preview['effect_key'],
                    'attempt_token':attempt['attempt_token'],'approval_hash':attempt['approval_hash'],
                    'actor':actor,'at':self.clock(),'result':result})
            self.repo.put_state('connectsafely_job',job['id'],{
                **current,'state':'done' if result.get('status')=='sent' else 'needs_reconciliation','result':result})
        return {**result,'simulation':self.simulation}

    def recover(self, body, actor):
        self._actor(actor)
        self._simulation_boundary()
        if not isinstance(body,dict) or 'preview_id' not in body:
            raise ConnectSafelyError('invalid_recovery')
        preview = self._preview(body['preview_id'])
        self._check_mode(preview)
        return self._console(preview['plan']['action']).recover_not_dispatched(body, actor=actor)

    def inbox(self, actor):
        self._actor(actor)
        self._simulation_boundary()
        if self.provider is None:
            raise ConnectSafelyError('adapter_not_configured')
        value = self.provider.inbox()
        return {**value, 'simulation':self.simulation}
