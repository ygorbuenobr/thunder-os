"""Offline copilot composition; no credentials, network, or runtime activation.

load_current is a trusted core reader returning current step/pack/version and
suppression/reply flags. It is re-read after preflight; production composition
must additionally preserve existing WorkerRuntime research/delegation gates.
Do not install this executor as WorkerRuntime's adapter (double reservation).
"""
from datetime import datetime, timezone
from .fallback_contract import ActionFallback, ActionIntent, FallbackBlocked
from .providers.result import ProviderResult
from .worker import EffectCoordinator


class _OneAttempt:
    def __init__(self, adapter):
        self.adapter = adapter

    def send(self, effect):
        try:
            return self.adapter.send(effect)
        except Exception:
            # A thrown transport error does not prove absence of a side effect.
            return ProviderResult(effect['effect_id'], 'uncertain', error_code='execution_unknown')


class CopilotFallbackExecutor:
    def __init__(self, repository, ledger, approvals, *, account_id, load_current,
                 adapters, preflights, enabled=False, clock=None):
        self.repo, self.ledger, self.approvals = repository, ledger, approvals
        self.account_id, self.load_current = account_id, load_current
        self.adapters, self.preflights = dict(adapters), dict(preflights)
        self.enabled = enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _current(self, step_id):
        snapshot = self.load_current(step_id)
        step, pack = snapshot['step'], snapshot['pack']
        if (step['id'] != step_id or step['channel'] != 'linkedin'
                or snapshot['version'] != pack.cadence_version
                or pack.actor != 'ygor' or snapshot['account_id'] != self.account_id):
            raise FallbackBlocked('current_scope_mismatch')
        if snapshot['suppressed'] or snapshot['has_reply']:
            raise FallbackBlocked('current_contact_blocked')
        auth = self.approvals.authorize_step(step, pack, self.clock())
        intent = ActionIntent('message', self.account_id, step['recipient'],
                              {'step': dict(step), 'approval_id': pack.id,
                               'cadence_id': pack.cadence_id, 'version': snapshot['version']},
                              auth.effect_hash, auth.effect_hash)
        return snapshot, intent

    def execute(self, step_id, *, primary, fallback=None):
        if not self.enabled:
            raise FallbackBlocked('copilot_execution_not_enabled')
        _, intent = self._current(step_id)

        def canonical(selection):
            current, current_intent = self._current(step_id)
            if current_intent.fingerprint() != selection.fingerprint:
                raise FallbackBlocked('current_scope_changed')
            adapter = self.adapters.get(selection.adapter)
            if adapter is None:
                raise FallbackBlocked('adapter_not_configured')
            result = EffectCoordinator(self.ledger, self.approvals,
                                       {'linkedin': _OneAttempt(adapter)}).execute_due(
                current['step'], current['pack'], self.clock(),
                suppressed=current['suppressed'], has_reply=current['has_reply'])
            return {'status': 'confirmed' if result.status == 'sent' else 'uncertain',
                    'receipt_ref': result.receipt_ref, 'error_code': result.error_code}

        router = ActionFallback(self.repo, preflights=self.preflights,
                                core_execute=canonical, enabled=True)
        return router.execute(router.prepare(intent, primary=primary, fallback=fallback))
