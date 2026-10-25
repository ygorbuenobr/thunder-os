"""Prepared provider stop ports and post-ReplyControl composition; runtime OFF.

Apollo reuses ApolloProspectReadonly for fresh same-key profile and complete
contact membership reads. The caller authorizes the exact command through existing authority. A single
attempt is reserved in the existing Repository state, as ExternalEmailImport does;
no new database, schema, approval type or automatic retry is introduced.
Default OFF rejects before credential loading. HTTP acceptance is never a stop.
Reconcile is a separate fresh read and does not repeat the mutation.

Official contracts checked 2026-10-25:
https://docs.apollo.io/reference/update-contact-status-sequence.md
https://docs.apollo.io/reference/search-for-contacts.md
https://developer.instantly.ai/api-reference/lead/patch-lead.md
https://developers.hubspot.com/docs/api-reference/latest/automation/sequences/guide.md
Instantly status is readOnly. Its update-interest-status operation can stop followups,
but changes commercial classification and may trigger automations/global status sync;
it is not a neutral individual pause and is not substituted here.
https://developer.instantly.ai/api-reference/lead/update-the-interest-status-of-a-lead
https://help.instantly.ai/en/articles/7913412-how-to-stop-sending-to-specific-leads
HubSpot guide does not document an unenroll operation and requires user-level
access; do not guess DELETE or use account-level credentials as that authority.
"""
from copy import deepcopy
import math
import re
from urllib.parse import urlencode

from .apollo_readonly import _https_request
from .apollo_prospect_readonly import ApolloProspectReadonly
from .models import payload_hash


class ExternalPauseError(ValueError):
    pass


def _require(value, reason):
    if not value:
        raise ExternalPauseError(reason)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def _email(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value) is not None


def capability(provider):
    reasons = {'instantly': 'neutral_pause_unverified_interest_update_has_commercial_effects',
               'hubspot': 'unenroll_contract_and_user_level_access_unverified',
               'hubspot_seq': 'unenroll_contract_and_user_level_access_unverified'}
    return {'provider': provider, 'implemented': provider == 'apollo',
            'runtime_enabled': False, 'paused': False,
            'reason': 'activation_and_canary_required' if provider == 'apollo'
                      else reasons.get(provider, 'provider_not_supported')}


class ApolloExternalPause:
    """Fixed-account/sequence adapter. No default credential discovery.

    binding={campaign_id,account_id,sequence_id}; command adds contact_id,email.
    repository owns the durable single attempt, including process restarts.
    authorize(command) is the existing exact-effect authorization callback, called
    before reads and again immediately before the one mutation. request is a
    trusted bounded transport with the same fixed-host contract as Apollo reads.
    """
    def __init__(self, binding, *, credential_loader=None, request=None,
                 mutation_enabled=False, read_enabled=False, authorize=None, timeout=10, repository=None):
        _require(isinstance(binding, dict) and set(binding) == {'campaign_id', 'account_id', 'sequence_id'}
                 and all(_identifier(v) for v in binding.values()), 'invalid_pause_binding')
        _require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 15,
                 'invalid_pause_timeout')
        self.repo = repository
        self.binding = deepcopy(binding)
        self.loader, self.request = credential_loader, request or _https_request
        self.mutation_enabled, self.read_enabled = mutation_enabled, read_enabled
        self.authorize, self.timeout = authorize, timeout

    def _command(self, command):
        _require(isinstance(command, dict) and set(command) == set(self.binding) | {'contact_id', 'email'}
                 and all(command.get(k) == v for k, v in self.binding.items())
                 and _identifier(command.get('contact_id')) and _email(command.get('email')),
                 'invalid_pause_target')
        return deepcopy(command)

    def _authority(self, command):
        _require(self.mutation_enabled is True, 'external_pause_disabled')
        try:
            valid = callable(self.authorize) and self.authorize(deepcopy(command)) is True
        except Exception:
            valid = False
        _require(valid, 'external_pause_not_authorized')

    def _credential(self):
        try:
            token = self.loader() if callable(self.loader) else None
        except Exception:
            token = None
        _require(isinstance(token, str) and 0 < len(token) <= 4096
                 and all(32 < ord(c) < 127 for c in token), 'pause_credential_unavailable')
        return token

    def _membership(self, command, token):
        # New reader each time: no stale profile cache, same credential as write.
        reader = ApolloProspectReadonly(lambda: token, request=self.request,
            enabled=True, timeout=self.timeout, max_rows=100)
        profile = reader.profile()
        _require(profile.get('complete') is True and profile.get('source', {}).get('identity_verified') is True
                 and profile['source'].get('account_id') == command['account_id'], 'pause_account_unverified')
        result = reader.contacts_search(command['email'], per_page=100)
        _require(result.get('complete') is True and result.get('source', {}).get('identity_verified') is True
                 and result['source'].get('account_id') == command['account_id'], 'pause_membership_incomplete')
        matches = [r for r in result.get('items', []) if r.get('id') == command['contact_id']
                   and isinstance(r.get('email'), str) and r['email'].lower() == command['email'].lower()]
        _require(len(matches) == 1 and matches[0].get('contact_campaign_statuses_complete') is True,
                 'pause_contact_unverified')
        statuses = [s for s in matches[0]['contact_campaign_statuses']
                    if s.get('emailer_campaign_id') == command['sequence_id']]
        _require(len(statuses) == 1, 'pause_sequence_unverified')
        return statuses[0]

    def stop(self, command):
        command = self._command(command)
        self._authority(command)
        _require(self.repo is not None, 'pause_repository_required')
        _require(getattr(self.repo._local, 'connection', None) is None, 'pause_outer_transaction_blocked')
        key = payload_hash({'provider': 'apollo', **{k: command[k] for k in
            ('account_id', 'sequence_id', 'contact_id')}})
        digest = payload_hash(command)
        with self.repo.transaction():
            prior = self.repo.get_state('external_pause_attempt', key)
            if prior:
                _require(prior['command_hash'] == digest, 'pause_idempotency_conflict')
            else:
                self.repo.append_state('external_pause_attempt', key,
                    {'command_hash': digest, 'status': 'reserved'})
        if prior:
            return {'provider': 'apollo', 'status': 'reconciliation_required', 'paused': False,
                    'write_attempted': False, 'readback_verified': False, 'retry_allowed': False,
                    'previous_status': prior['status'], 'target': command}
        try:
            result = self._stop_once(command, key)
        except Exception:
            self._attempt(key, 'needs_human')
            raise
        self._attempt(key, result['status'])
        return result

    def _attempt(self, key, status):
        self.repo.put_state('external_pause_attempt', key,
            {**self.repo.get_state('external_pause_attempt', key), 'status': status})

    def _stop_once(self, command, key):
        token = self._credential()
        state = self._membership(command, token)
        _require(state.get('status') in {'active', 'paused', 'finished'}, 'pause_state_unverified')
        if state['status'] == 'finished':
            return {'provider': 'apollo', 'status': 'already_stopped', 'paused': True,
                    'readback_verified': True, 'write_attempted': False, 'retry_allowed': False,
                    'source': 'fresh_contact_membership', 'target': command}
        self._authority(command)
        path = '/api/v1/emailer_campaigns/remove_or_stop_contact_ids?' + urlencode({
            'emailer_campaign_ids[]': command['sequence_id'], 'contact_ids[]': command['contact_id'],
            'mode': 'mark_as_finished'})
        result = {'provider': 'apollo', 'paused': False, 'readback_verified': False,
                  'write_attempted': True, 'retry_allowed': False, 'target': command}
        self._attempt(key, 'attempted')
        try:
            status, raw = self.request('POST', path, None,
                {'x-api-key': token, 'Content-Type': 'application/json'}, self.timeout)
        except Exception:
            return {**result, 'status': 'uncertain', 'reason': 'stop_request_uncertain'}
        # Do not retain response bodies: provider job payloads can echo credentials.
        if type(status) is not int or status >= 500 or status < 100:
            return {**result, 'status': 'uncertain', 'reason': 'stop_request_uncertain'}
        if 200 <= status < 300:
            return {**result, 'status': 'readback_required', 'http_status': status}
        return {**result, 'status': 'failed', 'reason': 'stop_request_rejected', 'http_status': status}

    def reconcile(self, command):
        command = self._command(command)
        _require(self.read_enabled is True, 'external_pause_read_disabled')
        try:
            state = self._membership(command, self._credential())
        except (ExternalPauseError, ValueError):
            return {'provider': 'apollo', 'status': 'unverified', 'paused': False,
                    'readback_verified': False, 'retry_allowed': False, 'write_attempted': False,
                    'reason': 'fresh_membership_unavailable', 'target': command}
        stopped = state.get('status') == 'finished'
        return {'provider': 'apollo', 'status': 'confirmed' if stopped else 'not_stopped',
                'paused': stopped, 'readback_verified': stopped, 'retry_allowed': False,
                'write_attempted': False, 'target': command,
                'source': 'fresh_contact_membership', 'observed_state': state.get('status')}


class UnsupportedExternalPause:
    """No transport fallback: unsupported individual stops remain manual actions."""
    def __init__(self, provider):
        self.provider = provider

    def stop(self, command):
        return {**capability(self.provider), 'status': 'blocked', 'write_attempted': False,
                'readback_verified': False, 'retry_allowed': False}

    reconcile = stop


class ApolloReplyPauseComposition:
    """Candidate bridge from committed ReplyControl data to the Apollo stop port.

    Default OFF; no server route or automatic reader activates it. The only input
    references are an existing reply, journey and source key. Target IDs come from
    the journey's exported canonical bindings, never browser-supplied IDs or CSV
    hints. Existing campaign context and exact authority are rechecked immediately
    before dispatch. The network is outside ReplyControl/Repository transactions.
    authorize(envelope) must use existing human/delegated authority; an accepted
    reply, export or campaign configuration alone never authorizes the stop.
    """
    def __init__(self, repository, registry, journey, hubspot, *, credential_loader=None,
                 request=None, execution_enabled=False, read_enabled=False, authorize=None):
        self.repo, self.registry, self.journey, self.hubspot = repository, registry, journey, hubspot
        self.loader, self.request = credential_loader, request
        self.execution_enabled, self.read_enabled, self.authorize = execution_enabled, read_enabled, authorize

    def _resolve(self, reply_id, journey_id, source_key):
        _require(getattr(self.repo._local, 'connection', None) is None,
                 'pause_outer_transaction_blocked')
        _require(self.journey.repo is self.repo, 'pause_repository_mismatch')
        reply = self.repo.get_state('reply', reply_id)
        draft = self.repo.get_state('journey_draft', journey_id)
        alert = self.repo.get_state('operator_alert', reply_id)
        _require(isinstance(reply, dict) and reply.get('control_source') == 'reply_control'
                 and reply.get('identity_confirmed') is True and reply.get('human_reply') is True
                 and isinstance(draft, dict) and isinstance(alert, dict)
                 and alert.get('reply_id') == reply_id and alert.get('control_source') == 'reply_control',
                 'pause_verified_reply_required')
        context = self.registry.load(reply['campaign_id'])
        binding = self.registry.binding(reply['campaign_id'])
        _require(context['platform'] == 'apollo' and draft.get('campaign_id') == reply['campaign_id']
                 and draft.get('campaign_context') == context and binding['providers'].get('email') == 'apollo',
                 'pause_campaign_context_changed')
        snapshot, sequence = draft.get('export_snapshot'), draft.get('sequence') or {}
        _require(isinstance(snapshot, dict) and snapshot.get('scope_hash') == self.journey._scope(draft)
                 and snapshot.get('sequence_id') == sequence.get('apollo_sequence_id')
                 and snapshot.get('account_id') == sequence.get('account_id')
                 and snapshot.get('account_id') == binding['provider_accounts'].get('email'),
                 'pause_export_binding_required')
        leads = [r for r in draft.get('rows', []) if r.get('source_key') == source_key]
        checks = [r for r in draft.get('checks', []) if r.get('source_key') == source_key]
        target = snapshot.get('recipient_bindings', {}).get(source_key)
        _require(len(leads) == 1 and len(checks) == 1 and source_key in snapshot.get('source_keys', [])
                 and isinstance(target, dict) and target.get('lookup_complete') is True
                 and _identifier(target.get('contact_id')), 'pause_recipient_binding_required')
        lead, qualification = leads[0], checks[0].get('qualification_context') or {}
        _require(qualification.get('contact_id') == reply['hubspot_contact_id']
                 and isinstance(qualification.get('binding_hash'), str)
                 and _email(lead.get('email')) and target.get('email') == lead['email'],
                 'pause_canonical_contact_mismatch')
        command = {'campaign_id': reply['campaign_id'], 'account_id': snapshot['account_id'],
                   'sequence_id': snapshot['sequence_id'], 'contact_id': target['contact_id'], 'email': lead['email']}
        envelope = {'operation': 'apollo_stop_for_reply', 'reply_id': reply_id,
                    'reply_input_hash': reply['input_hash'], 'reply_version': reply['version'],
                    'journey_id': journey_id, 'journey_version': draft['version'],
                    'source_key': source_key, 'hubspot_contact_id': reply['hubspot_contact_id'],
                    'campaign_context': context, 'command': command}
        return envelope, lead, qualification, alert

    def _fresh_identity(self, envelope, lead, qualification):
        from .new_journey_hubspot import HubSpotEligibilityRead
        from .new_journey_qualification import candidate
        evidence = HubSpotEligibilityRead(self.hubspot).lookup(lead['email'],
            contact_id=envelope['hubspot_contact_id'], profile=lead.get('linkedin_url'))
        fresh = candidate(evidence)
        _require(fresh is not None and fresh['contact_id'] == envelope['hubspot_contact_id']
                 and fresh['binding_hash'] == qualification['binding_hash'], 'pause_canonical_identity_changed')

    def _authorized(self, expected):
        if self.execution_enabled is not True or self.journey.service.global_paused():
            return False
        try:
            current, _, _, _ = self._resolve(expected['reply_id'], expected['journey_id'], expected['source_key'])
            if current != expected or not callable(self.authorize) or self.authorize(deepcopy(expected)) is not True:
                return False
            latest, _, _, _ = self._resolve(expected['reply_id'], expected['journey_id'], expected['source_key'])
            return (latest == expected and self.execution_enabled is True
                    and not self.journey.service.global_paused())
        except Exception:
            return False

    def _adapter(self, envelope):
        command = envelope['command']
        return ApolloExternalPause({k: command[k] for k in ('campaign_id', 'account_id', 'sequence_id')},
            repository=self.repo, credential_loader=self.loader, request=self.request,
            mutation_enabled=self.execution_enabled, read_enabled=self.read_enabled,
            authorize=lambda candidate: candidate == command and self._authorized(envelope))

    def dispatch(self, reply_id, journey_id, source_key):
        _require(self.execution_enabled is True, 'external_pause_disabled')
        envelope, lead, qualification, alert = self._resolve(reply_id, journey_id, source_key)
        _require(self._authorized(envelope), 'external_pause_not_authorized')
        self._fresh_identity(envelope, lead, qualification)
        result = self._adapter(envelope).stop(envelope['command'])
        return self._record(alert, result, envelope)

    def reconcile(self, reply_id, journey_id, source_key):
        _require(self.read_enabled is True, 'external_pause_read_disabled')
        envelope, lead, qualification, alert = self._resolve(reply_id, journey_id, source_key)
        self._fresh_identity(envelope, lead, qualification)
        result = self._adapter(envelope).reconcile(envelope['command'])
        return self._record(alert, result, envelope)

    def _record(self, alert, result, envelope):
        # This is a provider-specific observation, not confirmation of all channels.
        from .db import RepositoryConflict
        try:
            latest, _, _, _ = self._resolve(envelope['reply_id'], envelope['journey_id'], envelope['source_key'])
            current = latest == envelope
        except Exception:
            current = False
        if not current:
            return {**result, 'observation_persisted': False, 'context_current': False,
                    'all_channels_stopped': False}
        observation = {k: deepcopy(v) for k, v in result.items() if k != 'target'}
        observation['target'] = {k: result['target'][k] for k in
                                 ('account_id', 'sequence_id', 'contact_id', 'campaign_id')}
        observation['journey_id'] = envelope['journey_id']
        observation['journey_version'] = envelope['journey_version']
        value = {k: v for k, v in alert.items() if k not in {'id', 'version', 'updated_at'}}
        value['external_pause_observations'] = {**value.get('external_pause_observations', {}), 'apollo': observation}
        try:
            self.repo.put_states_atomic([('operator_alert', alert['id'], value, alert['version'])])
            persisted = True
        except RepositoryConflict:
            persisted = False
        return {**result, 'observation_persisted': persisted, 'context_current': True, 'all_channels_stopped': False}
