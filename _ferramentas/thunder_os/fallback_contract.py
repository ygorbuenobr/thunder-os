"""Per-action fallback contract. No provider, browser, credential or send implementation.

A trusted composition must delegate execution to the existing core authorization
and canonical touch ledger. This module may switch adapters ONLY during a
verified preflight with no attempted effect. Once selected, exactly one call to
core_execute is permitted; every ambiguous result requires reconciliation.
"""
from dataclasses import dataclass, asdict
from typing import Mapping
from .models import payload_hash
from .db import RepositoryConflict


ACTIONS = frozenset({'message', 'invite', 'comment', 'like'})
SAFE_PREFLIGHT = frozenset({'unavailable', 'unsupported'})
BLOCK_PREFLIGHT = frozenset({'auth_required', 'captcha', 'permission_denied', 'account_mismatch', 'rate_limited'})


class FallbackBlocked(ValueError):
    pass


@dataclass(frozen=True)
class ActionIntent:
    action: str
    account_id: str
    destination: str
    payload: Mapping
    effect_hash: str  # Exact approved core effect scope hash, shared by ALL adapters.
    idempotency_key: str  # Existing canonical core key; never adapter-specific.

    def fingerprint(self):
        if self.action not in ACTIONS:
            raise FallbackBlocked('action_not_supported')
        for value in (self.account_id, self.destination, self.effect_hash, self.idempotency_key):
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise FallbackBlocked('invalid_action_scope')
        if not isinstance(self.payload, Mapping) or not self.payload:
            raise FallbackBlocked('exact_payload_required')
        return payload_hash(asdict(self))


@dataclass(frozen=True)
class Preflight:
    status: str
    account_id: str
    destination: str
    effect_hash: str
    idempotency_key: str
    action: str
    attempted: bool = False


@dataclass(frozen=True)
class SelectedAction:
    adapter: str
    intent: ActionIntent
    fingerprint: str


class ActionFallback:
    def __init__(self, repository, *, preflights, core_execute=None, enabled=False):
        self.repo = repository
        self.preflights = dict(preflights)
        self.core_execute = core_execute
        self.enabled = enabled

    def prepare(self, intent, *, primary, fallback=None):
        """Read-only capability probes only; auth/CAPTCHA never select another route."""
        fingerprint = intent.fingerprint()
        for adapter in (primary, fallback):
            if adapter is None:
                break
            probe = self.preflights.get(adapter)
            if probe is None:
                # Absence is known before an attempt; do not pretend capability parity.
                continue
            try:
                result = probe(intent)
            except Exception:
                raise FallbackBlocked('preflight_unknown') from None
            if not isinstance(result, Preflight) or result.attempted is not False:
                raise FallbackBlocked('preflight_may_have_attempted')
            if (result.account_id, result.destination, result.effect_hash, result.idempotency_key, result.action) != (
                intent.account_id, intent.destination, intent.effect_hash, intent.idempotency_key, intent.action):
                raise FallbackBlocked('preflight_scope_mismatch')
            if result.status in BLOCK_PREFLIGHT:
                raise FallbackBlocked(result.status)
            if result.status == 'ready':
                return SelectedAction(adapter, intent, fingerprint)
            if result.status not in SAFE_PREFLIGHT:
                raise FallbackBlocked('preflight_unknown')
        raise FallbackBlocked('no_verified_adapter')

    def execute(self, selection):
        """Disabled by default. core_execute must enforce approval+canonical ledger.

        The injected callback receives the SAME intent regardless of selected
        adapter. It must not use adapter-specific dedupe or retry internally.
        This boundary never calls a second adapter after execution starts.
        """
        if not self.enabled or self.core_execute is None:
            raise FallbackBlocked('copilot_execution_not_enabled')
        if not isinstance(selection, SelectedAction) or selection.intent.fingerprint() != selection.fingerprint:
            raise FallbackBlocked('selection_scope_changed')
        key = selection.intent.idempotency_key
        payload = {'fingerprint': selection.fingerprint, 'adapter': selection.adapter,
                   'effect_hash': selection.intent.effect_hash, 'status': 'reserved'}
        try:
            self.repo.append_state('fallback_attempt', key, payload)
        except RepositoryConflict:
            previous = self.repo.get_state('fallback_attempt', key)
            if not previous or previous['fingerprint'] != selection.fingerprint:
                raise FallbackBlocked('idempotency_scope_mismatch') from None
            return {'status': 'needs_reconciliation' if previous['status'] == 'reserved' else previous['status'],
                    'receipt_ref': previous.get('receipt_ref'), 'error_code': previous.get('error_code'),
                    'replayed': True, 'fallback_allowed': False}
        try:
            result = self.core_execute(selection)
        except TimeoutError:
            result = {'status': 'uncertain', 'error_code': 'execution_timeout'}
        except Exception:
            result = {'status': 'uncertain', 'error_code': 'execution_unknown'}
        if not isinstance(result, Mapping):
            result = {'status': 'uncertain', 'error_code': 'invalid_execution_receipt'}
        status = result.get('status')
        receipt = result.get('receipt_ref')
        code = result.get('error_code')
        if status == 'confirmed' and isinstance(receipt, str) and 0 < len(receipt) <= 512 and not any(c.isspace() for c in receipt):
            status, code = 'confirmed', None
        elif status == 'blocked' and code in BLOCK_PREFLIGHT:
            status, receipt = 'blocked', None
        else:
            # Even a rejection after entering execution cannot trigger fallback.
            status, receipt = 'uncertain', None
            code = code if code in {'execution_timeout', 'execution_unknown', 'invalid_execution_receipt'} else 'execution_requires_reconciliation'
        stored = {**payload, 'status': status, 'receipt_ref': receipt, 'error_code': code,
                  'fallback_allowed': False}
        self.repo.put_state('fallback_attempt', key, stored)
        return {key: stored[key] for key in ('status', 'receipt_ref', 'error_code', 'fallback_allowed')}
