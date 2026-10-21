"""Internal reply stop bridge. Provider transports are deliberately absent."""
from collections.abc import Mapping
from datetime import datetime
import re
from zoneinfo import ZoneInfo

from .approvals import ApprovalError
from .cadence import CadenceEngine
from .models import payload_hash
from .replies import ReplyCoordinator


class ReplyControlError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(ok, code):
    if not ok:
        raise ReplyControlError(code)


def _text(value, limit=200):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 for c in value))


def external_pause_plan(provider):
    """Offline capability description, not an executable provider command."""
    operation = 'remove_or_stop_contact_ids' if provider == 'apollo' else None
    return {'provider': provider, 'status': 'off' if operation else 'blocked',
            'reason': 'external_pause_off' if operation else 'pause_capability_unverified',
            'operation': operation, 'mark_as_finished': True if operation else None,
            'readback_required': True, 'paused': False}


def assess_external_pause(result):
    """A generic port result is never evidence of a paused provider in this slice."""
    if not isinstance(result, Mapping):
        return 'external_pause_failed'
    if result.get('status') in {'failed', 'uncertain'}:
        return 'external_pause_failed'
    if result.get('http_status') == 202:
        return 'external_pause_readback_required'
    return 'external_pause_off'


class _ReplyStore:
    def __init__(self, repo):
        self.repo = repo

    def get(self, ident):
        return self.repo.get_state('reply', ident)

    def save(self, value):
        if value['identity_confirmed'] and value['human_reply'] is True:
            value.update(state='needs_classification', next_action='classificar resposta')
        return self.repo.append_state('reply', value['id'], value)


class _CadenceBridge:
    def __init__(self, repo, matches):
        self.repo, self.matches = repo, matches

    def pause_for_reply(self, person_key, reply_id):
        effects = self.repo.effects()
        for row in self.matches:
            protected = {e['payload'].get('step', {}).get('id') for e in effects
                         if e['payload'].get('cadence_id') == row['id']
                         and e['state'] in {'sent', 'uncertain', 'needs_reconciliation'}}
            steps = CadenceEngine().pause_for_reply(row['steps'])
            steps = [dict(old) if old.get('id') in protected else new
                     for old, new in zip(row['steps'], steps)]
            value = {k: v for k, v in row.items() if k not in {'id', 'version', 'updated_at'}}
            value.update(steps=steps, status='paused_reply', pause_reason='resposta recebida',
                         reply_id=reply_id)
            self.repo.put_states_atomic([('cadence', row['id'], value, row['version'])])


class ReplyControl:
    """Trusted internal API; HTTP authentication belongs to server composition.

    external_adapters accepts *prepared local diagnostic mappings*, never clients
    or callbacks. There is no external I/O, including within a caller transaction.
    """
    def __init__(self, repo, registry, external_adapters=None, external_pause_enabled=False):
        _require(external_pause_enabled is False, 'external_pause_activation_not_authorized')
        _require(external_adapters is None or isinstance(external_adapters, Mapping),
                 'invalid_external_adapters')
        self.repo, self.registry = repo, registry
        self.external_pause_enabled = False
        self.external_adapters = dict(external_adapters or {})
        _require(all(isinstance(v, Mapping) for v in self.external_adapters.values()),
                 'invalid_external_adapters')

    @staticmethod
    def _validate(payload, actor):
        fields = {'id', 'channel', 'content', 'received_at', 'hubspot_contact_id',
                  'campaign_id', 'human_reply', 'identity_verified', 'source'}
        _require(isinstance(payload, Mapping) and set(payload) == fields, 'invalid_reply_payload')
        _require(actor in {'ygor', 'verified_source'}, 'reply_trusted_entry_required')
        _require(_text(payload['id']), 'invalid_reply_id')
        _require(isinstance(payload['channel'], str) and payload['channel'] in {'email', 'linkedin', 'whatsapp', 'phone', 'chattie'},
                 'invalid_reply_channel')
        _require(isinstance(payload['content'], str) and bool(payload['content'].strip())
                 and len(payload['content']) <= 200_000, 'invalid_reply_content')
        _require(type(payload['identity_verified']) is bool
                 and (payload['human_reply'] is None or type(payload['human_reply']) is bool),
                 'invalid_reply_verification')
        try:
            _require(_text(payload['received_at'], 100), 'invalid_reply_time')
            stamp = datetime.fromisoformat(payload['received_at'])
            _require(stamp.tzinfo is not None, 'invalid_reply_time')
        except (ValueError, TypeError):
            raise ReplyControlError('invalid_reply_time') from None
        contact, campaign = payload['hubspot_contact_id'], payload['campaign_id']
        _require(contact is None or (isinstance(contact, str)
                 and re.fullmatch(r'[1-9][0-9]{0,29}', contact)), 'invalid_reply_contact')
        _require(campaign is None or (isinstance(campaign, str)
                 and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', campaign)), 'invalid_reply_campaign')
        source = payload['source']
        _require(isinstance(source, Mapping)
                 and set(source) == {'provider', 'account_id', 'provider_message_id'}
                 and all(_text(v) for v in source.values()), 'invalid_reply_source')
        _require(source['provider'] in {'manual', 'connectsafely', 'apollo', 'instantly',
                                      'hubspot', 'gmail', 'chattie'}, 'invalid_reply_source')
        if source['provider'] == 'manual':
            _require(actor == 'ygor' and source['account_id'] == 'ygor'
                     and payload['id'] in {source['provider_message_id'],
                                           'manual-' + source['provider_message_id']},
                     'reply_manual_authority_required')
        else:
            _require(actor == 'verified_source', 'reply_verified_source_required')

    def _identity(self, value):
        if not value['identity_verified'] or not value['hubspot_contact_id'] or not value['campaign_id']:
            return False
        try:
            binding = self.registry.binding(value['campaign_id'])
        except ApprovalError:
            return False
        source = value['source']
        if source['provider'] == 'manual':
            return True
        return any(provider == source['provider']
                   and binding['provider_accounts'].get(channel) == source['account_id']
                   for channel, provider in binding['providers'].items())

    def _matches(self, contact):
        keys = {contact, 'hubspot:' + contact}
        for row in self.repo.list_state('identity'):
            records = row.get('records', [])
            ids = {r.get('hubspot_contact_id') for r in records if isinstance(r, Mapping)
                   and r.get('hubspot_contact_id') is not None}
            if (row.get('status') == 'canonical' and row.get('actor') == 'ygor'
                    and ids == {contact} and _text(row.get('canonical_person'))):
                keys.add(row['canonical_person'])
        return [row for row in self.repo.list_state('cadence')
                if row.get('status') in {'draft', 'ready_for_review', 'approved', 'active',
                                         'blocked', 'needs_channel_decision', 'paused_reply'}
                and (row.get('hubspot_contact_id') == contact
                    or (row.get('hubspot_contact_id') is None and row.get('person_key') in keys))]

    def ingest(self, payload, actor):
        self._validate(payload, actor)
        _require(getattr(self.repo._local, 'connection', None) is None,
                 'reply_outer_transaction_blocked')
        value = dict(payload)
        value['source'] = dict(payload['source'])
        ident = 'reply-control:' + payload_hash(value['source'])
        fingerprint = payload_hash({k: v for k, v in value.items() if k not in {'id', 'received_at'}})
        with self.repo.transaction():
            existing = self.repo.get_state('reply', ident)
            if existing is not None:
                _require(existing.get('input_hash') == fingerprint, 'reply_event_conflict')
                return self._result(existing, duplicate=True)
            verified = self._identity(value)
            human = value['human_reply'] is True
            matches = self._matches(value['hubspot_contact_id']) if verified and human else []
            bridge = _CadenceBridge(self.repo, matches)
            incoming = {**value, 'id': ident, 'event_id': value['id'], 'actor': actor,
                        'input_hash': fingerprint, 'control_source': 'reply_control',
                        'identity_confirmed': verified, 'paused_cadence_ids': [r['id'] for r in matches],
                        'external_pauses_enabled': False}
            if matches:
                incoming.update(person_key=matches[0].get('person_key') or 'hubspot:' + value['hubspot_contact_id'],
                                account_slug=matches[0].get('account_id') or 'internal',
                                cadence_id=matches[0]['id'])
            # Coordinator owns correlation/state; the bridge adapts its signature.
            reply = ReplyCoordinator(_ReplyStore(self.repo), bridge).ingest(incoming)
            reason = ('identity_unresolved' if not verified else 'nonhuman_or_unknown_reply'
                      if not human else 'external_pause_off')
            if verified and human:
                platform = self.registry.load(value['campaign_id'])['platform']
                plan = self.external_adapters.get(platform, external_pause_plan(platform))
                reason = (assess_external_pause(plan) if platform in self.external_adapters
                          else plan['reason'])
            today = datetime.now(ZoneInfo('America/Sao_Paulo')).date().isoformat()
            alert = {'control_source': 'reply_control', 'reply_id': ident, 'active': True,
                     'priority': 'high', 'due_at': today, 'owner': 'ygor',
                     'title': 'Revisar resposta e parada dos canais', 'reason': reason,
                     'code': reason, 'content': value['content'], 'source': value['source'],
                     'hubspot_contact_id': value['hubspot_contact_id'], 'campaign_id': value['campaign_id'],
                     'message': 'Resposta registrada. Pausa externa nao confirmada; revisar canais.',
                     'next_action': ('Resolver identidade antes de agir.' if not verified else
                                     'Conferir se a resposta e humana antes de pausar.' if not human else
                                     'Revisar a conversa e conferir manualmente a parada em cada ferramenta.'),
                     'external_pauses_enabled': False}
            self.repo.append_state('operator_alert', ident, alert)
            return self._result(reply)

    def _result(self, reply, duplicate=False):
        return {'status': ('paused_internal' if reply['paused_cadence_ids'] else
                           'reply_recorded' if reply['identity_confirmed'] and reply['human_reply'] is True
                           else 'triage'),
                'reply': reply, 'duplicate': duplicate,
                'alerts': [self.repo.get_state('operator_alert', reply['id'])],
                'external_pause_enabled': False, 'external_pauses_enabled': False}

    def snapshot(self):
        with self.repo.transaction():
            replies = [r for r in self.repo.list_state('reply') if r.get('control_source') == 'reply_control']
            alerts = [r for r in self.repo.list_state('operator_alert')
                      if r.get('control_source') == 'reply_control' and r.get('active') is True]
            return {'status': 'internal_only', 'replies': replies, 'alerts': alerts,
                    'source': {'provider': 'repository', 'state_kinds': ['reply', 'cadence', 'operator_alert']},
                    'external_pause_enabled': False, 'external_pauses_enabled': False,
                    'automatic_reply_reader_connected': False}
