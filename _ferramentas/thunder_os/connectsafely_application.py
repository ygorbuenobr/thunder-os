"""One authenticated application facade and durable worker queue.

Composition is explicit. No environment, credentials, migrations, network probes
or provider selection happen here. Simulations are always labelled as such.
"""
import re
import time
from uuid import uuid4

from .connectsafely_console import ConnectSafelyError, SAFE_CODES
from .db import RepositoryConflict
from .models import payload_hash


class ConnectSafelyApplication:
    def __init__(self, repository, *, direct_console=None, social_console=None,
                 provider=None, simulation=False, execution_enabled=False,
                 readiness=None, action_readiness=None, execution_scope=None, clock=time.time, lease_seconds=120):
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

    def enqueue(self, body, actor):
        self._actor(actor)
        if (not isinstance(body, dict) or set(body) != {'preview_id','expected_version','review_hash'}
                or type(body.get('expected_version')) is not int):
            raise ConnectSafelyError('exact_review_required')
        with self.repo.transaction():
            preview = self._preview(body['preview_id'])
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
                    'expires_at':preview['expires_at'], 'execution_scope':self.execution_scope, 'approved_at':self.clock()})
                existing = self.repo.append_state('connectsafely_job', key, {
                    'preview_id':preview['id'], 'review':dict(body), 'actor':'ygor',
                    'state':'queued', 'simulation':self.simulation, 'execution_scope':self.execution_scope, 'created_at':self.clock()})
            return {'job_id':key, 'preview_id':preview['id'], 'state':existing['state'], 'simulation':self.simulation}

    def _job_authorized(self,job):
        self._check_mode(self._preview(job['preview_id']))
        approval = self.repo.get_state('connectsafely_queue_approval', job['id'])
        if (not approval or approval.get('actor') != 'ygor'
                or approval.get('review') != job.get('review')
                or approval.get('simulation') is not self.simulation
                or approval.get('expires_at',0) <= self.clock()
                or (self.execution_scope is not None and (job.get('execution_scope') != self.execution_scope
                    or approval.get('execution_scope') != self.execution_scope))):
            raise ConnectSafelyError('job_authority_invalid')

    def _claim(self, job_id=None):
        if self.execution_scope is not None and job_id is None:
            return None
        with self.repo.transaction():
            jobs=([self.repo.get_state('connectsafely_job',self._id(job_id))] if job_id is not None
                  else self.repo.list_state('connectsafely_job'))
            for job in jobs:
                if job is None or (self.execution_scope is not None and job.get('execution_scope') != self.execution_scope):
                    continue
                if self.execution_scope is not None:
                    self._job_authorized(job)
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
            self._enabled(action)
            job = self._claim(job_id)
        except ConnectSafelyError:
            return False
        if job is None:
            return False
        try:
            if job.get('actor') != 'ygor' or job.get('simulation') is not self.simulation:
                raise ConnectSafelyError('job_authority_invalid')
            self._job_authorized(job)
            preview = self._preview(job['preview_id'])
            def dispatch_guard():
                self._enabled(preview['plan']['action'])
                self._job_authorized(job)
                with self.repo.transaction():
                    current = self.repo.get_state('connectsafely_job', job['id'])
                    if (not current or current['state'] != 'running'
                            or current.get('claim_token') != job['claim_token']
                            or current['lease_until'] <= self.clock()
                            or current['review'] != job['review']):
                        raise ConnectSafelyError('worker_lease_expired')
                    self.repo.put_state('connectsafely_job', job['id'], {**current, 'dispatch_started':True})
                return True
            result = self._console(preview['plan']['action']).execute(
                job['review'], actor='ygor', dispatch_guard=dispatch_guard)
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
        result = self._console(preview['plan']['action']).reconcile(body, actor=actor)
        return {**result, 'simulation':self.simulation}

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
