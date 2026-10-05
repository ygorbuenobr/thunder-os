"""Read-only projections of trusted integration configuration and local drafts.

No credential/environment access, network, probes, provider calls or writes. The
composition root injects observations from independently performed reads; a read
observation is never evidence of write readiness or permission. Defaults fail
closed and historical observations expire. Do not populate observations with
fixtures in a live composition root.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from .integration_hubspot import FIELDS, PORTAL_ID

PROVIDERS = ('hubspot', 'connectsafely', 'seqd_email')


@dataclass(frozen=True)
class IntegrationRuntime:
    read_configured: bool = False
    write_configured: bool = False
    write_authorized: bool = False
    external_effects_enabled: bool = False
    approval_mode_selected: bool = False


@dataclass(frozen=True)
class ProbeObservation:
    checked_at: datetime
    outcome: str  # verified_read | unavailable; never a raw provider response


class IntegrationViews:
    def __init__(self, repository=None, *, runtime=None, observations=None, clock=None,
                 evidence_ttl=timedelta(minutes=5)):
        if not isinstance(evidence_ttl, timedelta) or not timedelta(0) < evidence_ttl <= timedelta(minutes=5):
            raise ValueError('bounded evidence TTL required')
        self.repository = repository
        self.runtime = dict(runtime or {})
        self.observations = dict(observations or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.evidence_ttl = evidence_ttl

    def status(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError('aware clock required')
        integrations = []
        for provider in PROVIDERS:
            config = self.runtime.get(provider)
            config = config if isinstance(config, IntegrationRuntime) else IntegrationRuntime()
            observation = self.observations.get(provider)
            valid = (isinstance(observation, ProbeObservation)
                     and isinstance(observation.checked_at, datetime) and observation.checked_at.tzinfo is not None
                     and observation.outcome in {'verified_read', 'unavailable'}
                     and observation.checked_at <= now)
            recent = valid and now - observation.checked_at <= self.evidence_ttl
            verified = bool(recent and observation.outcome == 'verified_read')
            blockers = []
            if provider == 'connectsafely':
                blockers.append('consult_connectsafely_application')
            if not config.read_configured:
                blockers.append('read_adapter_not_configured')
            if not valid:
                state = 'not_verified'
                blockers.append('read_not_verified')
            elif not recent:
                state = 'stale'
                blockers.append('read_verification_expired')
            elif observation.outcome == 'unavailable':
                state = 'unavailable'
                blockers.append('provider_unavailable')
            else:
                state = 'read_verified'
            if not config.write_configured:
                blockers.append('write_adapter_not_configured')
            if not config.write_authorized:
                blockers.append('write_not_authorized')
            if not config.external_effects_enabled:
                blockers.append('external_effects_disabled')
            if provider != 'hubspot' and not config.approval_mode_selected:
                blockers.append('approval_mode_pending')
            if provider != 'hubspot':
                blockers.append('outbound_executor_not_available')
            # Read success and a wired transport alone never authorize writes.
            can_write = (provider == 'hubspot' and verified and config.read_configured is True and config.write_configured is True
                         and config.write_authorized is True and config.external_effects_enabled is True
                         and (provider == 'hubspot' or config.approval_mode_selected is True))
            integrations.append({
                'id': provider, 'state': state,
                'last_checked_at': observation.checked_at.isoformat() if valid else None,
                'last_verified_read_at': observation.checked_at.isoformat() if valid and observation.outcome == 'verified_read' else None,
                'capabilities': {'read': verified and config.read_configured is True,
                                 'prepare_draft': provider == 'hubspot' and verified and config.read_configured is True, 'write': bool(can_write), 'delete': False},
                'blockers': blockers,
            })
        return {'schema_version': 1, 'read_only': True, 'integrations': integrations}

    def drafts(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('draft limit must be between 1 and 100')
        if self.repository is None:
            return {'schema_version': 1, 'read_only': True, 'status': 'not_configured', 'drafts': [], 'hubspot_attempts': [], 'errors': []}
        values, attempts, errors = [], [], []
        for kind, provider in (('gmail_draft', 'seqd_email'), ('inbox_draft', 'unipile')):
            try:
                rows = self.repository.list_state(kind)
                for row in reversed(rows):
                    if not isinstance(row, Mapping) or row.get('status') != 'local_draft':
                        continue
                    content = row.get('content')
                    if not isinstance(content, str) or len(content) > 10000:
                        continue
                    values.append({'id': str(row.get('id', ''))[:150], 'provider': provider,
                        'status': 'local_draft', 'content': content, 'can_execute': False,
                        'requires_current_read_and_review': True})
                    if len(values) >= limit:
                        break
            except Exception:
                errors.append({'source': kind, 'code': 'local_source_unavailable'})
            if len(values) >= limit:
                break
        try:
            rows = self.repository.list_state('hubspot_attempt')
            for row in reversed(rows):
                if not isinstance(row, Mapping):
                    continue
                plan = row.get('plan')
                if not isinstance(plan, Mapping) or plan.get('portal_id') != PORTAL_ID:
                    continue
                kind = plan.get('object_type')
                if kind not in FIELDS or plan.get('operation') not in {'create', 'update'}:
                    continue
                status = row.get('status')
                if status not in {'reserved', 'attempted', 'response_received', 'no_write', 'needs_human', 'verified_state'}:
                    status = 'needs_reconciliation'
                attempts.append({'id': str(row.get('id', ''))[:150], 'provider': 'hubspot',
                    'object_type': kind, 'operation': plan['operation'], 'status': status,
                    'write_attribution_confirmed': False, 'can_execute': False})
                if len(attempts) >= limit:
                    break
        except Exception:
            errors.append({'source': 'hubspot_attempt', 'code': 'local_source_unavailable'})
        return {'schema_version': 1, 'read_only': True, 'status': 'partial' if errors else 'ok',
                'drafts': values, 'hubspot_attempts': attempts, 'errors': errors}
