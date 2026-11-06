"""Preventive Apollo stop from a persisted, correlated LinkedIn reply.

Not installed in a worker or HTTP handler; external mutation is off by default.
The Apollo stop mode has no verified membership readback contract, so every
attempt remains pending and must never be represented as confirmed.
"""
from __future__ import annotations
from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import re
import time

from .apollo_linkedin_identity_binding import KIND, binding_key
from .apollo_linkedin_reply_coverage import ApolloContactReplyReader
from .apollo_linkedin_runtime_safety import RuntimeSafety
from .external_pause_adapters import ApolloExternalPause, _identifier
from .models import payload_hash


class _ApolloContactStop(ApolloExternalPause):
    """Stop by contact ID only when the authenticated detail proves membership."""
    def __init__(self, *args, contact_reader, **kwargs):
        super().__init__(*args, **kwargs)
        self.contact_reader = contact_reader

    def _command(self, command):
        if (not isinstance(command, dict) or set(command) != set(self.binding) | {'contact_id'}
                or any(command.get(k) != v for k, v in self.binding.items())
                or not _identifier(command.get('contact_id'))):
            raise ValueError('invalid_pause_target')
        return dict(command)

    def _membership(self, command, token):
        # Construct the final identity read with the exact token used for the
        # subsequent POST; a separately configured reader may use another key.
        reader = ApolloContactReplyReader(lambda: token, team_id=self.contact_reader.team_id,
            api_actor_id=self.contact_reader.api_actor_id, request=self.contact_reader._request,
            timeout=self.contact_reader.timeout)
        contact = reader.contact(command['contact_id'])
        statuses = contact.get('contact_campaign_statuses') if isinstance(contact, dict) else None
        if (not isinstance(contact, dict) or contact.get('id') != command['contact_id']
                or contact.get('contact_campaign_statuses_complete') is not True
                or not isinstance(statuses, list)):
            raise ValueError('pause_membership_incomplete')
        matches = [item for item in statuses if isinstance(item, dict)
                   and item.get('emailer_campaign_id') == command['sequence_id']]
        if len(matches) != 1:
            raise ValueError('pause_sequence_unverified')
        return matches[0]


class ApolloLinkedInReplyPause:
    def __init__(self, repo, *, contact_reader, credential_loader, request=None,
                 enabled=False):
        if type(enabled) is not bool or not callable(credential_loader):
            raise ValueError('configuration_invalid')
        self.repo, self.reader, self.loader = repo, contact_reader, credential_loader
        self.request, self.enabled = request, enabled

    def _resolve(self, reply_id):
        reply = self.repo.get_state('reply', reply_id)
        if (not isinstance(reply, dict) or reply.get('control_source') != 'reply_control'
                or reply.get('identity_confirmed') is not True
                or reply.get('identity_basis') != 'verified_connectsafely_event'
                or reply.get('human_reply') is False):
            raise ValueError('verified_reply_required')
        bind_id, event_id = reply.get('identity_binding_id'), reply.get('provider_event_id')
        binding = self.repo.get_state(KIND, bind_id)
        event = self.repo.get_state('connectsafely_event', event_id)
        alert = self.repo.get_state('operator_alert', reply_id)
        if (not isinstance(binding, dict) or binding.get('version') != 1
                or not isinstance(event, dict) or event.get('state') != 'processed'
                or event.get('reply_id') != reply_id or not isinstance(alert, dict)
                or alert.get('reply_id') != reply_id):
            raise ValueError('reply_binding_unavailable')
        if binding_key(binding['company'], binding['campaign_id'], binding['task_id'],
                       binding['account_id'], binding['approval_id']) != bind_id:
            raise ValueError('reply_binding_invalid')
        raw = event.get('event')
        source = reply.get('source')
        if (not isinstance(raw, dict) or event.get('fingerprint') != payload_hash(raw)
                or not isinstance(source, dict) or source.get('provider') != 'connectsafely'
                or source.get('account_id') != binding['account_id']
                or raw.get('account_id') != binding['account_id']
                or raw.get('sender_urn') != binding['member_urn']
                or raw.get('message_urn') != source.get('provider_message_id')
                or event_id != payload_hash({'account': binding['account_id'], 'message_urn': raw.get('message_urn')})
                or reply.get('campaign_id') != binding['campaign_id']
                or reply.get('apollo_contact_id') != binding['contact_id']):
            raise ValueError('reply_binding_mismatch')
        approval = self.repo.get_state('approval', binding['approval_id'])
        snapshot = self.repo.get_state('campaign_version_snapshot', 'apollo-campaign-' + binding['scope_hash'])
        scope = snapshot.get('scope') if isinstance(snapshot, dict) else None
        context = scope.get('context') if isinstance(scope, dict) else None
        config = scope.get('configuration') if isinstance(scope, dict) else None
        approved = config.get('apollo_linkedin') if isinstance(config, dict) else None
        if (not isinstance(approval, dict) or type(approval.get('version')) is not int
                or approval['version'] < 1 or approval.get('scope') != 'campaign_version'
                or approval.get('actor') != 'ygor' or approval.get('campaign_id') != binding['campaign_id']
                or approval.get('campaign_version') != binding['campaign_version']
                or approval.get('scope_hash') != binding['scope_hash']
                or approval.get('snapshot_id') != 'apollo-campaign-' + binding['scope_hash']
                or not isinstance(snapshot, dict) or snapshot.get('version') != 1
                or snapshot.get('scope_hash') != binding['scope_hash'] or payload_hash(scope) != binding['scope_hash']
                or not isinstance(context, dict) or context.get('platform') != 'apollo'
                or context.get('campaign_version') != binding['campaign_version']
                or not isinstance(approved, dict)
                or any(approved.get(k) != binding[v] for k, v in (
                    ('company_id', 'company'), ('team_id', 'team_id'),
                    ('sequence_id', 'sequence_id'), ('account_id', 'account_id')))):
            raise ValueError('campaign_binding_changed')
        row = self.repo.get_apollo_linkedin_reservation(binding['company'], binding['task_id'],
                                                        'linkedin_step_message')
        if (not isinstance(row, dict) or row.get('state') != 'confirmed'
                or row.get('account_id') != binding['account_id']
                or row.get('recipient') != binding['recipient_public_id']
                or row.get('campaign_version') != str(binding['campaign_version'])
                or not isinstance(row.get('receipt'), dict) or not row['receipt'].get('provider_id')):
            raise ValueError('receipt_unavailable')
        attempts = [event for event in self.repo.events(binding['task_id'])
                    if event.get('type') == 'apollo_linkedin_campaign_reservation']
        expected = {'binding_id': bind_id, 'company': binding['company'],
                    'campaign_id': binding['campaign_id'], 'approval_id': binding['approval_id'],
                    'approval_scope_hash': binding['scope_hash'], 'task_id': binding['task_id'],
                    'account_id': binding['account_id'], 'apollo_contact_id': binding['contact_id'],
                    'apollo_team_id': binding['team_id'], 'apollo_sequence_id': binding['sequence_id'],
                    'member_urn': binding['member_urn'], 'action_type': 'linkedin_step_message'}
        if (len(attempts) != 1 or not isinstance(attempts[0].get('payload'), dict)
                or any(attempts[0]['payload'].get(k) != v for k, v in expected.items())):
            raise ValueError('reservation_binding_unverified')
        sent = datetime.fromisoformat(raw['sent_at'].replace('Z', '+00:00'))
        started = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
        if sent.tzinfo is None or started.tzinfo is None or sent < started:
            raise ValueError('event_time_invalid')
        stop_id = RuntimeSafety._key({'company_id': binding['company']},
                                     {'recipient_public_id': binding['recipient_public_id']})
        stop = self.repo.get_state('apollo_linkedin_reply_stop', stop_id)
        if not isinstance(stop, dict) or stop.get('active') is not True:
            raise ValueError('local_stop_unavailable')
        return binding, alert

    def _pending(self, reply_id, reason):
        alert = self.repo.get_state('operator_alert', reply_id)
        if not isinstance(alert, dict):
            raise ValueError('operator_alert_unavailable')
        value = {k: v for k, v in alert.items() if k not in ('id', 'version', 'updated_at')}
        value['external_pause_status'] = 'external_pause_pending'
        value['external_pause_reason'] = reason
        self.repo.put_states_atomic([('operator_alert', reply_id, value, alert['version'])])

    def stop(self, reply_id):
        if self.enabled is not True or os.environ.get('THUNDER_APOLLO_LI_REPLY_STOP_ENABLED') != 'true':
            return {'status': 'blocked', 'reason': 'external_pause_disabled'}
        attempt_key = None
        adapter_entered = False
        try:
            binding, _ = self._resolve(reply_id)
            self._pending(reply_id, 'awaiting_external_confirmation')
            if (type(self.reader) is not ApolloContactReplyReader
                    or self.reader.team_id != binding['team_id']):
                raise ValueError('contact_reader_unavailable')
            contact = self.reader.contact(binding['contact_id'])
            email = contact.get('email') if isinstance(contact, dict) else None
            command = {'campaign_id': binding['campaign_id'], 'account_id': binding['team_id'],
                       'sequence_id': binding['sequence_id'], 'contact_id': binding['contact_id']}
            attempt_key = payload_hash({'provider': 'apollo', **{k: command[k] for k in
                ('account_id', 'sequence_id', 'contact_id')}})
            if isinstance(email, str) and email:
                command['email'] = email
            def authorized(current):
                try:
                    latest, _ = self._resolve(reply_id)
                    return (self.enabled is True and os.environ.get('THUNDER_APOLLO_LI_REPLY_STOP_ENABLED') == 'true'
                            and latest == binding and current == command)
                except Exception:
                    return False
            cls = ApolloExternalPause if 'email' in command else _ApolloContactStop
            extra = {} if 'email' in command else {'contact_reader': self.reader}
            adapter = cls({k: command[k] for k in ('campaign_id', 'account_id', 'sequence_id')},
                credential_loader=self.loader, request=self.request, repository=self.repo,
                mutation_enabled=True, read_enabled=True, authorize=authorized, mode='stop', **extra)
            adapter_entered = True
            result = adapter.stop(command)
            self._pending(reply_id, result.get('reason') or result.get('status', 'unverified'))
            attempt = self.repo.get_state('external_pause_attempt', attempt_key)
            retryable = isinstance(attempt, dict) and attempt.get('status') == 'pre_dispatch_blocked'
            return {'status': 'external_pause_pending', 'write_attempted': result.get('write_attempted') is True,
                    'readback_verified': False, 'retry_allowed': retryable}
        except Exception:
            try:
                self._pending(reply_id, 'pause_unavailable_or_unverified')
            except Exception:
                pass
            try:
                attempt = self.repo.get_state('external_pause_attempt', attempt_key) if attempt_key else None
                retryable = isinstance(attempt, dict) and attempt.get('status') == 'pre_dispatch_blocked'
            except Exception:
                retryable = False
            return {'status': 'external_pause_pending',
                    'write_attempted': False if retryable or not adapter_entered else None,
                    'readback_verified': False, 'retry_allowed': retryable}


class ApolloReplyPauseWorker:
    """Process at most one correlated reply per bounded worker pass.

    Delivery is fenced by the adapter's durable attempt state; no provider
    calls are made by the webhook. Pending/ambiguous provider writes are never
    retried. Time-limited pre-dispatch failures can be considered again.
    """
    def __init__(self, port, *, company, team, clock=time.time, backoff_seconds=60):
        if (not all(isinstance(v, str) and v for v in (company, team))
                or not callable(clock) or type(backoff_seconds) is not int or backoff_seconds < 30):
            raise ValueError('worker_configuration_invalid')
        self.port, self.repo, self.clock = port, port.repo, clock
        self.company, self.team = company, team
        self.backoff_seconds, self.cursor = backoff_seconds, ''
        self._next_scan = 0.0

    def process_one(self):
        now = self.clock()
        if (os.environ.get('THUNDER_APOLLO_LI_REPLY_STOP_ENABLED') != 'true'
                or self.port.enabled is not True or now < self._next_scan):
            return False
        self._next_scan = now + 5
        with self.repo._connect() as con:
            rows = con.execute("SELECT entity_id,payload FROM thunder_state WHERE kind='reply' "
                "AND entity_id>? AND json_extract(payload,'$.identity_basis')='verified_connectsafely_event' "
                "ORDER BY entity_id LIMIT 50", (self.cursor,)).fetchall()
        if not rows:
            self.cursor = ''
            return False
        for row in rows:
            reply_id = row['entity_id']
            self.cursor = reply_id
            reply = json.loads(row['payload'])
            alert = self.repo.get_state('operator_alert', reply_id)
            binding = self.repo.get_state(KIND, reply.get('identity_binding_id'))
            if (not isinstance(alert, dict) or not isinstance(binding, dict)
                    or binding.get('company') != self.company or binding.get('team_id') != self.team):
                continue
            key = payload_hash({'provider': 'apollo', 'account_id': binding.get('team_id'),
                'sequence_id': binding.get('sequence_id'), 'contact_id': binding.get('contact_id')})
            attempt = self.repo.get_state('external_pause_attempt', key)
            if isinstance(attempt, dict) and attempt.get('status') != 'pre_dispatch_blocked':
                continue
            # Durable alert/attempt timestamps bound retries across restarts.
            delayed = attempt or (alert if alert.get('external_pause_status') == 'external_pause_pending' else None)
            if delayed is not None:
                try:
                    updated = datetime.fromisoformat(delayed['updated_at'].replace('Z', '+00:00')).timestamp()
                    if updated > now or now - updated < self.backoff_seconds:
                        continue
                except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
                    continue
            self.port.stop(reply_id)
            return True
        return False


def main(argv=None):
    """Run one explicitly selected containment action; never schedules itself."""
    parser = argparse.ArgumentParser(description='Parada preventiva Apollo por resposta LinkedIn')
    parser.add_argument('--reply', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9:_-]{1,160}', args.reply):
        result = {'status': 'blocked', 'reason': 'reply_invalid'}
    elif not args.execute:
        result = {'status': 'dry_run', 'write_attempted': False, 'enabled': False}
    elif os.environ.get('THUNDER_APOLLO_LI_REPLY_STOP_ENABLED') != 'true':
        result = {'status': 'blocked', 'reason': 'external_pause_disabled', 'write_attempted': False}
    else:
        try:
            from .credential_paths import credential_path
            from .connectsafely_live import credential
            from .db import Repository, _official_apollo_linkedin_db
            path = Path(os.environ.get('PAINEL_DB', ''))
            official = _official_apollo_linkedin_db()
            if (not path.is_absolute() or not path.is_file() or not official.is_file()
                    or path.resolve() != official.resolve()):
                raise ValueError('official_store_unavailable')
            repo = Repository(path)
            repo.apollo_linkedin_store_ready()
            reply = repo.get_state('reply', args.reply)
            binding = repo.get_state(KIND, reply['identity_binding_id'])
            actor = os.environ.get('THUNDER_APOLLO_LI_API_ACTOR', '')
            if (not isinstance(binding, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', actor)
                    or os.environ.get('THUNDER_APOLLO_LI_COMPANY') != binding.get('company')
                    or os.environ.get('THUNDER_APOLLO_LI_TEAM') != binding.get('team_id')):
                raise ValueError('runtime_binding_invalid')
            loader = credential(credential_path('apollo_api_key', '/root/.config/thunder/credentials/apollo_api_key.txt'))
            reader = ApolloContactReplyReader(loader, team_id=binding['team_id'], api_actor_id=actor)
            result = ApolloLinkedInReplyPause(repo, contact_reader=reader, credential_loader=loader,
                                             enabled=True).stop(args.reply)
        except Exception:
            result = {'status': 'external_pause_pending', 'reason': 'runtime_unavailable',
                      'write_attempted': None, 'readback_verified': False}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'dry_run' else 2


if __name__ == '__main__':
    raise SystemExit(main())
