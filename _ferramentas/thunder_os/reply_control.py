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
    status = result.get('status')
    # A malformed diagnostic must not roll back the internal reply/stop transaction.
    if status is not None and not isinstance(status, str):
        return 'external_pause_failed'
    if status in {'failed', 'uncertain'}:
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
        channel, provider = value['channel'], source['provider']
        if (channel == 'email' and provider == 'hubspot'
                and binding['providers'].get('hubspot') == 'hubspot'
                and binding['provider_accounts'].get('hubspot') == source['account_id']):
            return True
        supported = {'email': {'apollo', 'instantly', 'gmail', 'hubspot'},
                     'linkedin': {'connectsafely', 'chattie'}}
        return (provider in supported.get(channel, set())
                and binding['providers'].get(channel) == provider
                and binding['provider_accounts'].get(channel) == source['account_id'])

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

    def _provider_match(self, value, correlation):
        # Trusted runtime entry only: revalidate persisted receipt ownership inside
        # the same transaction as the pause. No network or CRM identity inference.
        from .email_readback import GmailReceiptReader
        _require(isinstance(correlation, Mapping) and set(correlation) == {'receipt', 'binding'},
                 'reply_provider_correlation_invalid')
        _require(value['source']['provider'] == 'gmail' and value['channel'] == 'email'
                 and value['identity_verified'] is True and value['hubspot_contact_id'] is None,
                 'reply_provider_correlation_invalid')
        current = GmailReceiptReader(self.repo)._binding(correlation['receipt'])
        _require(current == correlation['binding'], 'reply_provider_binding_changed')
        job, cadence, context, binding = current
        _require(cadence.get('hubspot_contact_id') is None
                 and context['campaign_id'] == value['campaign_id']
                 and binding['provider_accounts']['email'] == value['source']['account_id']
                 and _text(cadence.get('person_key')), 'reply_provider_correlation_invalid')
        return cadence

    def _connectsafely_match(self, value, correlation, *, allow_processed=False):
        """Correlate only an authenticated durable webhook to an immutable binding."""
        from .apollo_linkedin_identity_binding import KIND, binding_key
        from .apollo_linkedin_runtime_safety import RuntimeSafety
        _require(isinstance(correlation, Mapping) and set(correlation) == {'binding_id', 'event_id'}
                 and all(_text(correlation[k]) for k in correlation), 'reply_provider_correlation_invalid')
        _require(value['source']['provider'] == 'connectsafely' and value['channel'] == 'linkedin'
                 and value['identity_verified'] is True and value['hubspot_contact_id'] is None
                 and value['human_reply'] is None, 'reply_provider_correlation_invalid')
        binding = self.repo.get_state(KIND, correlation['binding_id'])
        event_row = self.repo.get_state('connectsafely_event', correlation['event_id'])
        event_states = ('received', 'processed') if allow_processed else ('received',)
        _require(isinstance(binding, dict) and binding.get('version') == 1
                 and isinstance(event_row, dict) and event_row.get('state') in event_states,
                 'reply_provider_binding_unavailable')
        fields = ('company', 'campaign_id', 'approval_id', 'campaign_version', 'scope_hash', 'team_id',
                  'sequence_id', 'task_id', 'contact_id', 'account_id', 'recipient_public_id', 'member_urn')
        _require(all(_text(binding.get(k), 512) for k in fields), 'reply_provider_binding_invalid')
        _require(binding_key(binding['company'], binding['campaign_id'], binding['task_id'],
                             binding['account_id'], binding['approval_id']) == correlation['binding_id']
                 and value['campaign_id'] == binding['campaign_id']
                 and binding.get('action_type') in ('linkedin_step_message', 'linkedin_step_connect'),
                 'reply_provider_binding_mismatch')
        event = event_row.get('event')
        source = value['source']
        _require(isinstance(event, dict) and event_row.get('fingerprint') == payload_hash(event)
                 and correlation['event_id'] == payload_hash({'account': source['account_id'],
                                                               'message_urn': source['provider_message_id']})
                 and source['account_id'] == binding['account_id'] == event.get('account_id')
                 and source['provider_message_id'] == event.get('message_urn')
                 and event.get('sender_urn') == binding['member_urn']
                 and isinstance(event.get('content'), str)
                 and value['content'] == (event['content'] or '[mensagem sem texto; revisar contexto]'),
                 'reply_provider_event_mismatch')
        try:
            actual = datetime.fromisoformat(event['sent_at'].replace('Z', '+00:00'))
            supplied = datetime.fromisoformat(value['received_at'].replace('Z', '+00:00'))
            _require(actual.tzinfo is not None and actual == supplied, 'reply_provider_event_mismatch')
        except (KeyError, AttributeError, TypeError, ValueError):
            raise ReplyControlError('reply_provider_event_mismatch') from None
        approval = self.repo.get_state('approval', binding['approval_id'])
        snapshot = self.repo.get_state('campaign_version_snapshot', 'apollo-campaign-' + binding['scope_hash'])
        scope = snapshot.get('scope') if isinstance(snapshot, dict) else None
        approved_binding = scope.get('configuration', {}).get('apollo_linkedin') if isinstance(scope, dict) else None
        _require(isinstance(approval, dict) and type(approval.get('version')) is int
                 and approval['version'] >= 1
                 and approval.get('scope') == 'campaign_version' and approval.get('actor') == 'ygor'
                 and approval.get('campaign_id') == binding['campaign_id']
                 and approval.get('campaign_version') == binding['campaign_version']
                 and approval.get('scope_hash') == binding['scope_hash']
                 and approval.get('snapshot_id') == 'apollo-campaign-' + binding['scope_hash']
                 and isinstance(snapshot, dict) and snapshot.get('version') == 1
                 and snapshot.get('scope_hash') == binding['scope_hash'] and payload_hash(scope) == binding['scope_hash']
                 and isinstance(approved_binding, dict)
                 and all(approved_binding.get(k) == binding[v] for k, v in (
                     ('company_id', 'company'), ('team_id', 'team_id'), ('sequence_id', 'sequence_id'),
                     ('account_id', 'account_id'))), 'reply_provider_approval_unverified')
        reservation = self.repo.get_apollo_linkedin_reservation(binding['company'], binding['task_id'],
                                                                 binding['action_type'])
        _require(isinstance(reservation, dict) and reservation.get('state') == 'confirmed'
                 and reservation.get('account_id') == binding['account_id']
                 and reservation.get('recipient') == binding['recipient_public_id']
                 and reservation.get('campaign_version') == str(binding['campaign_version'])
                 and isinstance(reservation.get('receipt'), dict)
                 and _text(reservation['receipt'].get('provider_id')),
                 'reply_provider_reservation_unverified')
        expected_event = {'binding_id': correlation['binding_id'], 'company': binding['company'],
                          'campaign_id': binding['campaign_id'], 'approval_id': binding['approval_id'],
                          'approval_scope_hash': binding['scope_hash'], 'task_id': binding['task_id'],
                          'action_type': binding['action_type'], 'account_id': binding['account_id'],
                          'apollo_contact_id': binding['contact_id'], 'apollo_team_id': binding['team_id'],
                          'apollo_sequence_id': binding['sequence_id'], 'member_urn': binding['member_urn']}
        attempts = [row for row in self.repo.events(binding['task_id'])
                    if row.get('type') == 'apollo_linkedin_campaign_reservation']
        _require(len(attempts) == 1 and isinstance(attempts[0].get('payload'), dict)
                 and all(attempts[0]['payload'].get(k) == v for k, v in expected_event.items())
                 and str(attempts[0]['payload'].get('campaign_version')) == str(binding['campaign_version']),
                 'reply_provider_reservation_binding_unverified')
        try:
            started = datetime.fromisoformat(reservation['created_at'].replace('Z', '+00:00'))
            _require(started.tzinfo is not None and actual >= started, 'reply_provider_event_before_reservation')
        except (KeyError, AttributeError, TypeError, ValueError):
            raise ReplyControlError('reply_provider_event_before_reservation') from None
        duplicates = [row for row in self.repo.list_state(KIND)
                      if row.get('account_id') == binding['account_id']
                      and row.get('member_urn') == binding['member_urn']
                      and (row.get('company'), row.get('campaign_id'), row.get('contact_id')) !=
                          (binding['company'], binding['campaign_id'], binding['contact_id'])]
        _require(not duplicates, 'reply_provider_binding_ambiguous')
        stop_key = RuntimeSafety._key({'company_id': binding['company']},
                                    {'recipient_public_id': binding['recipient_public_id']})
        return binding, stop_key, not bool(event['content'])

    def promote_connectsafely_event(self, event_id, binding_id):
        """Attach proven identity to an untouched legacy reply, without duplication.

        Historic provider identity alone is not commercial identity: the reply
        remains in identity_resolution and external pause remains unconfirmed.
        """
        _require(_text(event_id) and _text(binding_id), 'reply_provider_correlation_invalid')
        _require(getattr(self.repo._local, 'connection', None) is None, 'reply_outer_transaction_blocked')
        with self.repo.transaction():
            pending = self.repo.get_state('connectsafely_correlation_pending', event_id)
            event_row = self.repo.get_state('connectsafely_event', event_id)
            binding = self.repo.get_state('apollo_linkedin_identity_binding', binding_id)
            _require(isinstance(pending, dict) and isinstance(event_row, dict) and isinstance(binding, dict),
                     'reply_provider_binding_unavailable')
            if pending.get('state') == 'resolved':
                _require(pending.get('binding_id') == binding_id and event_row.get('reply_id') == pending.get('reply_id'),
                         'reply_provider_binding_mismatch')
                reply = self.repo.get_state('reply', pending['reply_id'])
                _require(isinstance(reply, dict), 'reply_provider_binding_unavailable')
                return {'status': 'promoted', 'reply': reply, 'duplicate': True}
            event = event_row.get('event')
            expected_reply = 'connectsafely:' + event_id
            _require(pending.get('state') == 'pending' and pending.get('event_id') == event_id
                     and event_row.get('state') == 'processed' and event_row.get('reply_id') == expected_reply
                     and isinstance(event, dict) and pending.get('account_id') == event.get('account_id'),
                     'reply_provider_event_unavailable')
            legacy = self.repo.get_state('reply', expected_reply)
            expected_content = event.get('content') or '[mensagem sem texto; revisar contexto]'
            _require(isinstance(legacy, dict) and legacy.get('version') == 1
                     and legacy.get('actor') == 'connectsafely-inbox' and legacy.get('channel') == 'linkedin'
                     and legacy.get('content') == expected_content and legacy.get('state') == 'identity_resolution'
                     and legacy.get('classification_history') == [] and legacy.get('next_action') == 'resolver identidade'
                     and set(legacy) == {'id', 'version', 'updated_at', 'channel', 'content', 'actor', 'state',
                                         'classification_history', 'next_action'}, 'reply_legacy_modified')
            value = {'id': expected_reply, 'channel': 'linkedin', 'content': expected_content,
                     'received_at': event.get('sent_at'), 'hubspot_contact_id': None,
                     'campaign_id': binding.get('campaign_id'), 'human_reply': None, 'identity_verified': True,
                     'source': {'provider': 'connectsafely', 'account_id': event.get('account_id'),
                                'provider_message_id': event.get('message_urn')}}
            self._validate(value, 'verified_source')
            verified, stop_key, content_unavailable = self._connectsafely_match(
                value, {'binding_id': binding_id, 'event_id': event_id}, allow_processed=True)
            stop = self.repo.get_state('apollo_linkedin_reply_stop', stop_key)
            if stop is None:
                self.repo.append_state('apollo_linkedin_reply_stop', stop_key, {
                    'company': verified['company'], 'recipient': verified['recipient_public_id'],
                    'active': True, 'reason': 'unclassified_inbound_message'})
                self.repo.append_event(stop_key, 'apollo_linkedin_reply_stop', {
                    'reason': 'unclassified_inbound_message'})
            else:
                _require(stop.get('active') is True and stop.get('company') == verified['company']
                         and stop.get('recipient') == verified['recipient_public_id'], 'reply_stop_conflict')
            _require(self.repo.get_state('operator_alert', expected_reply) is None, 'reply_alert_conflict')
            reply = self.repo.put_state('reply', expected_reply, {
                **{k: v for k, v in legacy.items() if k not in ('id', 'version', 'updated_at')},
                'identity_confirmed': True, 'identity_basis': 'verified_connectsafely_event',
                'control_source': 'reply_control',
                'identity_binding_id': binding_id, 'provider_event_id': event_id,
                'company_id': verified['company'], 'campaign_id': verified['campaign_id'],
                'apollo_contact_id': verified['contact_id'], 'human_reply': None,
                'received_at': value['received_at'], 'source': value['source'],
                'content_unavailable': content_unavailable, 'external_pauses_enabled': False})
            self.repo.append_state('operator_alert', expected_reply, {
                'control_source': 'reply_control', 'reply_id': expected_reply, 'active': True,
                'priority': 'high', 'owner': 'ygor', 'due_at': datetime.now(ZoneInfo('America/Sao_Paulo')).date().isoformat(),
                'title': 'Revisar resposta e parada dos canais', 'content': expected_content,
                'source': value['source'], 'company_id': verified['company'], 'campaign_id': verified['campaign_id'],
                'reason': 'classification_and_external_pause_unconfirmed',
                'message': 'Mensagem recebida. Parada local ativa; parada externa no Apollo nao confirmada.',
                'next_action': 'Vincular a identidade comercial, classificar a mensagem e confirmar a parada no Apollo.',
                'external_pauses_enabled': False})
            self.repo.put_state('connectsafely_correlation_pending', event_id, {
                'account_id': verified['account_id'], 'event_id': event_id, 'state': 'resolved',
                'binding_id': binding_id, 'reply_id': expected_reply})
            return {'status': 'promoted', 'reply': reply, 'duplicate': False}

    def ingest(self, payload, actor, *, provider_correlation=None):
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
            linkedin_binding = None
            correlated = None
            if provider_correlation is not None:
                if isinstance(provider_correlation, Mapping) and set(provider_correlation) == {'binding_id', 'event_id'}:
                    linkedin_binding, stop_key, content_unavailable = self._connectsafely_match(value, provider_correlation)
                else:
                    correlated = self._provider_match(value, provider_correlation)
            verified = linkedin_binding is not None or correlated is not None or self._identity(value)
            human = value['human_reply'] is True
            matches = ([correlated] if correlated is not None else
                       self._matches(value['hubspot_contact_id'])) if verified and human else []
            if correlated is None and linkedin_binding is None and verified and human:
                accounts = {row.get('account_id') for row in matches}
                if len(accounts) != 1 or not _text(next(iter(accounts))):
                    verified = False
                    matches = []
            bridge = _CadenceBridge(self.repo, matches)
            incoming = {**value, 'id': ident, 'event_id': value['id'], 'actor': actor,
                        'input_hash': fingerprint, 'control_source': 'reply_control',
                        'identity_confirmed': verified, 'paused_cadence_ids': [r['id'] for r in matches],
                        'external_pauses_enabled': False}
            if correlated is not None:
                incoming['identity_basis'] = 'verified_provider_receipt'
                incoming['provider_receipt'] = provider_correlation['receipt']
            if linkedin_binding is not None:
                incoming['identity_basis'] = 'verified_connectsafely_event'
                incoming['identity_binding_id'] = provider_correlation['binding_id']
                incoming['provider_event_id'] = provider_correlation['event_id']
                incoming['apollo_contact_id'] = linkedin_binding['contact_id']
                incoming['company_id'] = linkedin_binding['company']
                incoming['content_unavailable'] = content_unavailable
                stop = self.repo.get_state('apollo_linkedin_reply_stop', stop_key)
                if stop is None:
                    self.repo.append_state('apollo_linkedin_reply_stop', stop_key, {
                        'company': linkedin_binding['company'],
                        'recipient': linkedin_binding['recipient_public_id'],
                        'active': True, 'reason': 'unclassified_inbound_message'})
                    self.repo.append_event(stop_key, 'apollo_linkedin_reply_stop', {
                        'reason': 'unclassified_inbound_message'})
                else:
                    _require(stop.get('active') is True and stop.get('company') == linkedin_binding['company']
                             and stop.get('recipient') == linkedin_binding['recipient_public_id'],
                             'reply_stop_conflict')
            if matches:
                incoming.update(person_key=matches[0].get('person_key') or 'hubspot:' + value['hubspot_contact_id'],
                                account_slug=matches[0].get('account_id') or 'internal',
                                cadence_id=matches[0]['id'])
            # Coordinator owns correlation/state; the bridge adapts its signature.
            reply = ReplyCoordinator(_ReplyStore(self.repo), bridge).ingest(incoming)
            reason = ('identity_unresolved' if not verified else 'nonhuman_or_unknown_reply'
                      if not human else 'external_pause_off')
            if linkedin_binding is not None:
                reason = 'classification_and_external_pause_unconfirmed'
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
                     'next_action': ('Resolver identidade e confirmar origem e canal antes de agir.' if not verified else
                                     'Conferir se a resposta e humana antes de pausar.' if not human else
                                     'Revisar a conversa e conferir manualmente a parada em cada ferramenta.'),
                     'external_pauses_enabled': False}
            if linkedin_binding is not None:
                alert['message'] = ('Mensagem recebida. Parada local ativa; parada externa no Apollo nao confirmada.')
                alert['next_action'] = ('Vincular a identidade comercial, classificar a mensagem e confirmar a parada no Apollo.')
            self.repo.append_state('operator_alert', ident, alert)
            if linkedin_binding is not None:
                webhook = self.repo.get_state('connectsafely_event', provider_correlation['event_id'])
                _require(isinstance(webhook, dict) and webhook.get('state') == 'received',
                         'reply_provider_event_unavailable')
                self.repo.put_state('connectsafely_event', provider_correlation['event_id'], {
                    **{k: v for k, v in webhook.items() if k not in ('id', 'version', 'updated_at')},
                    'state': 'processed', 'reply_id': ident})
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
