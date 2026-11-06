"""Apollo task readback port with completion blocked; no runtime composition.

An HTTP response does not by itself prove completion. The caller must persist a
durable confirmed LinkedIn receipt and its completion attempt before POST, and
must reconcile ambiguous attempts by reads only. This adapter alone is not an
authorization boundary and is not wired to the panel, a worker or a CLI.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import ssl
from datetime import datetime, timedelta, timezone

from .apollo_linkedin_attention import EXCLUDED_TASK
from .apollo_linkedin_preview import _json, _slug, validated_task_text
from .apollo_linkedin_authority import CampaignApprovalAuthority
from .models import payload_hash
from .db import Repository
from .apollo_linkedin_reply_coverage import coverage_current

_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_TASK = re.compile(r'/api/v1/tasks/([A-Za-z0-9_-]{1,128})(/complete)?\Z')

_GUARD = 'apollo_linkedin_completion_guard'


def completion_guard_key(company, task_id, action_type):
    return payload_hash({'company': company, 'task_id': task_id, 'action_type': action_type})


def skip_claim_key(company, task_id):
    return payload_hash({'company': company, 'task_id': task_id, 'action_type': 'linkedin_step_message'})


def write_completion_guard(repository, company, task_id, action_type, receipt_id, status):
    """Bind post-dispatch coverage to a receipt; blocked states cannot auto-clear."""
    if (status not in ('pending', 'clear', 'reply', 'unknown')
            or not isinstance(receipt_id, str) or not receipt_id.strip()):
        raise ValueError('completion_guard_invalid')
    key = completion_guard_key(company, task_id, action_type)
    with repository.transaction():
        previous = repository.get_state(_GUARD, key)
        identity = {'company': company, 'task_id': task_id, 'action_type': action_type,
                    'receipt_hash': payload_hash({'provider_id': receipt_id})}
        if previous is None:
            if status != 'pending':
                raise ValueError('completion_guard_missing')
        elif (any(previous.get(k) != v for k, v in identity.items())
              or previous.get('status') != 'pending' or status == 'pending'):
            raise ValueError('completion_guard_transition_invalid')
        repository.put_state(_GUARD, key, {**identity, 'status': status,
            'checked_at': datetime.now(timezone.utc).isoformat() if status == 'clear' else None})


def require_completion_guard(repository, company, task_id, action_type, receipt_id):
    guard = repository.get_state(_GUARD, completion_guard_key(company, task_id, action_type))
    if (not isinstance(guard, dict) or guard.get('status') != 'clear'
            or guard.get('company') != company or guard.get('task_id') != task_id
            or guard.get('action_type') != action_type
            or guard.get('receipt_hash') != payload_hash({'provider_id': receipt_id})):
        raise ValueError('completion_guard_blocked')
    try:
        checked = datetime.fromisoformat(guard['checked_at'])
        age = datetime.now(timezone.utc) - checked
        if checked.tzinfo is None or not timedelta(0) <= age <= timedelta(seconds=30):
            raise ValueError('completion_guard_expired')
    except (KeyError, TypeError, OverflowError):
        raise ValueError('completion_guard_expired') from None


def _request(method, path, body, headers, timeout):
    """Fixed-host GET-only transport; mutation remains structurally blocked."""
    match = _TASK.fullmatch(path) if isinstance(path, str) else None
    if match and match.group(1) == EXCLUDED_TASK:
        raise ValueError('fixture_protegida')
    # The real mutation path is deliberately unavailable until an approved
    # campaign binding and a shared pause fence exist. No flag enables it.
    allowed = (method == 'GET' and body is None and
               (path == '/api/v1/users/api_profile' or (match and not match.group(2))))
    if not allowed:
        raise ValueError('endpoint_blocked')
    if os.environ.get('SSL_CERT_FILE') or os.environ.get('SSL_CERT_DIR'):
        raise ValueError('tls_override_blocked')
    connection = http.client.HTTPSConnection('api.apollo.io', 443, timeout=timeout,
                                             context=ssl.create_default_context())
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if not isinstance(raw, bytes) or len(raw) > 1_000_000:
            raise ValueError('response_invalid')
        return response.status, raw
    finally:
        connection.close()


def _complete_request(method, path, body, headers, timeout):
    """Write only a completion note to the fixed Apollo endpoint."""
    match = _TASK.fullmatch(path) if isinstance(path, str) else None
    if not match or not match.group(2) or method != 'POST':
        raise ValueError('endpoint_blocked')
    if match.group(1) == EXCLUDED_TASK:
        raise ValueError('fixture_protegida')
    if not isinstance(body, bytes) or len(body) > 1024:
        raise ValueError('body_invalid')
    data = _json(body)
    if set(data) != {'note'} or not isinstance(data['note'], str) or not data['note'].startswith('Thunder OS: enviado via ConnectSafely, recibo '):
        raise ValueError('body_invalid')
    if os.environ.get('SSL_CERT_FILE') or os.environ.get('SSL_CERT_DIR'):
        raise ValueError('tls_override_blocked')
    connection = http.client.HTTPSConnection('api.apollo.io', 443, timeout=timeout,
                                             context=ssl.create_default_context())
    try:
        connection.request('POST', path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if not isinstance(raw, bytes) or len(raw) > 1_000_000:
            raise ValueError('response_invalid')
        return response.status, raw
    finally:
        connection.close()


def _skip_request(method, path, body, headers, timeout):
    match = re.fullmatch(r'/api/v1/tasks/([A-Za-z0-9_-]{1,128})/skip', path) if isinstance(path, str) else None
    if not match or method != 'POST' or match.group(1) == EXCLUDED_TASK:
        raise ValueError('endpoint_blocked')
    if not isinstance(body, bytes) or _json(body) != {'note': 'não conectado, pulado'}:
        raise ValueError('body_invalid')
    if os.environ.get('SSL_CERT_FILE') or os.environ.get('SSL_CERT_DIR'):
        raise ValueError('tls_override_blocked')
    connection = http.client.HTTPSConnection('api.apollo.io', 443, timeout=timeout,
                                             context=ssl.create_default_context())
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if not isinstance(raw, bytes) or len(raw) > 1_000_000:
            raise ValueError('response_invalid')
        return response.status, raw
    finally:
        connection.close()


class ApolloLinkedInCompletion:
    def __init__(self, credential_loader, *, team_id, actor_id, repository,
                 enabled=False, request=None, timeout=8, coverage=None, safety=None):
        if (not callable(credential_loader) or type(repository) is not Repository
                or not all(isinstance(v, str) and _ID.fullmatch(v) for v in (team_id, actor_id))
                or type(enabled) is not bool or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 15
                or (request is not None and not callable(request))):
            raise ValueError('configuration_invalid')
        self._loader, self._repo = credential_loader, repository
        self._request, self._enabled = request or _request, enabled
        self._post_request = request or _complete_request
        self._skip_post_request = request or _skip_request
        self.team_id, self.actor_id, self.timeout = team_id, actor_id, timeout
        # These ports are supplied by the runtime factory, not by a completion call.
        self._coverage, self._safety = coverage, safety

    @staticmethod
    def _task_id(task_id):
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            raise ValueError('invalid_task_id')
        if task_id == EXCLUDED_TASK:
            raise ValueError('fixture_protegida')
        return task_id

    def _read(self, path):
        token = self._loader()
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError('credential_unavailable')
        status, raw = self._request('GET', path, None,
            {'x-api-key': token, 'Accept': 'application/json'}, self.timeout)
        if status in (404, 422) and path.startswith('/api/v1/tasks/'):
            raise ValueError('task_removed')
        if status != 200:
            raise ValueError('apollo_unavailable')
        return _json(raw)

    def _verify_team(self):
        data = self._read('/api/v1/users/api_profile')
        user = data.get('user', data)
        if not isinstance(user, dict) or user.get('id') != self.actor_id or user.get('team_id') != self.team_id:
            raise ValueError('team_mismatch')

    def get(self, task_id):
        task_id = self._task_id(task_id)
        self._verify_team()
        task = self._read('/api/v1/tasks/' + task_id).get('task')
        if not isinstance(task, dict) or task.get('id') != task_id:
            raise ValueError('task_mismatch')
        return task

    def _execution_allowed(self):
        if not self._enabled or os.environ.get('THUNDER_APOLLO_LI_EXECUTION_ENABLED') != 'true':
            return False
        control = self._repo.get_state('control', 'global')
        return isinstance(control, dict) and control.get('paused') is False

    def skip_unconnected(self, company, task_id, *, campaign_id, approval_id,
                         campaign_version, account_id, before_dispatch):
        """Claim one Apollo-only skip; unknown POSTs are never retried."""
        self._task_id(task_id)
        if not all(isinstance(v, str) and _ID.fullmatch(v) for v in
                   (company, campaign_id, approval_id, account_id)) or not callable(before_dispatch):
            return {'state': 'blocked', 'reason': 'skip_binding_invalid'}
        key = skip_claim_key(company, task_id)
        identity = {'company': company, 'task_id': task_id, 'campaign_id': campaign_id,
                    'approval_id': approval_id, 'campaign_version': campaign_version, 'account_id': account_id}
        try:
            if not self._execution_allowed():
                return {'state': 'blocked', 'reason': 'execution_disabled'}
            self._verify_team()
            with self._repo.transaction():
                previous = self._repo.get_state('apollo_linkedin_skip', key)
                if previous is not None:
                    return {'state': 'skip_reconcile_pending', 'reason': 'skip_already_attempted'}
                if not self._execution_allowed() or before_dispatch() is not True:
                    return {'state': 'blocked', 'reason': 'skip_gate_unverified'}
                self._repo.put_state('apollo_linkedin_skip', key, {**identity, 'state': 'attempted'})
            token = self._loader()
            if not isinstance(token, str) or not token or any(c.isspace() for c in token):
                raise ValueError('credential_unavailable')
            body = json.dumps({'note': 'não conectado, pulado'}, ensure_ascii=False).encode()
            with self._repo.transaction():
                if not self._execution_allowed() or before_dispatch() is not True:
                    return {'state': 'skip_reconcile_pending', 'reason': 'skip_gate_unverified'}
                status, raw = self._skip_post_request('POST', '/api/v1/tasks/' + task_id + '/skip', body,
                    {'x-api-key': token, 'Accept': 'application/json', 'Content-Type': 'application/json'}, self.timeout)
                result = _json(raw)
                if status != 200 or result.get('deleted') is True or (isinstance(result.get('task'), dict)
                        and result['task'].get('deleted') is True):
                    raise ValueError('skip_unverified')
            task = self.get(task_id)
            if (task.get('deleted') is True or task.get('status') != 'skipped'
                    or task.get('note') != 'não conectado, pulado'):
                raise ValueError('skip_unverified')
            self._repo.put_state('apollo_linkedin_skip', key, {**identity, 'state': 'confirmed'})
            return {'state': 'skipped', 'reason': 'not_connected'}
        except Exception:
            return {'state': 'skip_reconcile_pending', 'reason': 'skip_unverified'}

    def reconcile_skip(self, company, task_id, *, campaign_id, approval_id,
                       campaign_version, account_id, binding, recipient):
        """Resolve an attempted skip only by scoped Apollo readback; never POST."""
        try:
            key = skip_claim_key(company, self._task_id(task_id))
            claim = self._repo.get_state('apollo_linkedin_skip', key)
            identity = {'company': company, 'task_id': task_id, 'campaign_id': campaign_id,
                        'approval_id': approval_id, 'campaign_version': campaign_version, 'account_id': account_id}
            if not isinstance(claim, dict) or any(claim.get(k) != v for k, v in identity.items()):
                raise ValueError('skip_binding_mismatch')
            task = self.get(task_id)
            if (task.get('deleted') is True or task.get('type') != 'linkedin_step_message'
                    or task.get('status') != 'skipped' or task.get('note') != 'não conectado, pulado'
                    or task.get('emailer_campaign_id') != binding.get('sequence_id')
                    or task.get('user_id') not in binding.get('owner_ids', ())
                    or not isinstance(task.get('contact'), dict)
                    or _slug(task['contact'].get('linkedin_url')) != recipient):
                raise ValueError('skip_unverified')
            return {'state': 'skipped', 'reason': 'not_connected'}
        except Exception:
            return {'state': 'skip_reconcile_pending', 'reason': 'skip_unverified'}

    def complete(self, company, task_id, action_type):
        """Fail closed: a verified approved binding and shared pause fence are absent."""
        self._task_id(task_id)
        if (not isinstance(company, str) or not _ID.fullmatch(company)
                or action_type != 'linkedin_step_connect'):
            return {'state': 'blocked', 'reason': 'reservation_invalid'}
        try:
            if not self._execution_allowed():
                return {'state': 'blocked', 'reason': 'execution_disabled'}
        except Exception:
            return {'state': 'blocked', 'reason': 'execution_disabled'}
        # A confirmed receipt alone does not prove company/team/sequence/owner
        # match the approved version; global pause can race a network call.
        # Do not claim a completion attempt or call the provider.
        return {'state': 'blocked', 'reason': 'execution_binding_unverified'}

    def reconcile(self, company, task_id, action_type):
        """Observe Apollo without mutating it or asserting an unverified binding."""
        task_id = self._task_id(task_id)
        try:
            row = self._repo.get_apollo_linkedin_reservation(company, task_id, action_type)
            if (not isinstance(row, dict) or row.get('state') != 'confirmed'
                    or row.get('apollo_status') not in ('pending', 'attempted')
                    or not isinstance(row.get('receipt'), dict) or not row['receipt']):
                return {'state': 'blocked', 'reason': 'reconciliation_not_pending'}
            with self._repo.apollo_linkedin_account_lock(row['account_id']):
                task = self.get(task_id)
                if task.get('status') != 'completed':
                    return {'state': 'apollo_reconcile_pending', 'reason': 'completion_unverified'}
                # Status alone does not establish the task still belongs to
                # the approved company/sequence/owner/version. Preserve the
                # pending state until that canonical binding is verifiable.
                return {'state': 'apollo_reconcile_pending', 'reason': 'execution_binding_unverified'}
        except Exception:
            return {'state': 'apollo_reconcile_pending', 'reason': 'completion_unverified'}

    def _historical_binding(self, campaign_id, approval_id, campaign_version):
        """Read the exact approved snapshot without applying current send controls."""
        approval = self._repo.get_state('approval', approval_id)
        if (not isinstance(approval, dict) or approval.get('version') != 1
                or approval.get('scope') != 'campaign_version'
                or approval.get('campaign_id') != campaign_id
                or approval.get('campaign_version') != campaign_version
                or approval.get('actor') != 'ygor'):
            raise ValueError('approval_unverified')
        digest = approval.get('scope_hash')
        if not isinstance(digest, str) or approval.get('snapshot_id') != 'apollo-campaign-' + digest:
            raise ValueError('approval_unverified')
        snapshot = self._repo.get_state('campaign_version_snapshot', approval['snapshot_id'])
        scope = snapshot.get('scope') if isinstance(snapshot, dict) else None
        if (not isinstance(snapshot, dict) or snapshot.get('version') != 1
                or not isinstance(scope, dict) or set(scope) != {'context', 'configuration', 'campaign_config'}
                or snapshot.get('scope_hash') != digest or payload_hash(scope) != digest
                or not isinstance(scope['context'], dict)
                or scope['context'].get('platform') != 'apollo'
                or scope['context'].get('campaign_version') != campaign_version
                or not isinstance(scope['configuration'], dict)
                or not isinstance(scope['configuration'].get('apollo_linkedin'), dict)):
            raise ValueError('approval_unverified')
        return scope['configuration']['apollo_linkedin']

    def _verified_binding(self, company, task_id, action_type, campaign_id, approval_id, campaign_version, *, historical=False):
        """Read durable approval, reservation and provider task; fail closed."""
        if action_type not in ('linkedin_step_message', 'linkedin_step_connect') or (not historical and not self._execution_allowed()):
            raise ValueError('execution_disabled')
        if historical:
            binding = self._historical_binding(campaign_id, approval_id, campaign_version)
        else:
            approval = CampaignApprovalAuthority(self._repo).require(campaign_id, approval_id, campaign_version)
            binding = approval['binding']
        row = self._repo.get_apollo_linkedin_reservation(company, task_id, action_type)
        if (not isinstance(row, dict) or row.get('state') != 'confirmed'
                or row.get('apollo_status') not in ('pending', 'attempted')
                or row.get('campaign_version') != str(campaign_version)
                or binding.get('company_id') != company or binding.get('team_id') != self.team_id
                or binding.get('account_id') != row.get('account_id')):
            raise ValueError('execution_binding_unverified')
        receipt = row.get('receipt')
        receipt_id = receipt.get('provider_id') if isinstance(receipt, dict) else None
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 256 or any(ord(c) < 32 for c in receipt_id):
            raise ValueError('receipt_unverified')
        task = self.get(task_id)
        if (task.get('deleted') is True or task.get('type') != action_type or task.get('emailer_campaign_id') != binding.get('sequence_id')
                or not isinstance(binding.get('owner_ids'), list) or task.get('user_id') not in binding['owner_ids']
                or not isinstance(task.get('contact'), dict)
                or _slug(task['contact'].get('linkedin_url')) != row.get('recipient')):
            raise ValueError('execution_binding_unverified')
        action = 'message' if action_type == 'linkedin_step_message' else 'invite'
        _, text_hash, blocker = validated_task_text(task, action)
        if blocker or text_hash != row.get('text_hash'):
            raise ValueError('text_changed')
        return row, task, receipt_id

    def _reply_clear(self, row, task, binding):
        coverage, safety = self._coverage, self._safety
        plan = getattr(coverage, 'plan', None)
        contact = task.get('contact')
        if (not isinstance(plan, dict) or not isinstance(contact, dict)
                or plan.get('account_id') != row.get('account_id')
                or plan.get('account_public_id') != binding.get('public_id')
                or plan.get('recipient_public_id') != row.get('recipient')
                or plan.get('action') != ('invite' if task.get('type') == 'linkedin_step_connect' else 'dm')
                or not isinstance(plan.get('recipient_profile_urn'), str)
                or not isinstance(contact.get('id'), str) or not contact['id']
                or task.get('contact_id') != contact['id']):
            return False
        # A previously persisted reply stop remains authoritative even if a
        # subsequent provider read is empty or a message disappears upstream.
        stop_key = safety._key(binding, plan)
        if self._repo.get_state('apollo_linkedin_reply_stop', stop_key) is not None:
            return False
        target = {'email': contact.get('email'), 'apollo_contact_id': contact['id'],
                  'sequence_id': binding.get('sequence_id'),
                  'linkedin_profile_urn': plan['recipient_profile_urn'],
                  'account_id': row['account_id'], 'recipient_public_id': row['recipient']}
        observed = coverage.observe(target)
        if observed.get('status') == 'reply':
            safety.pause_after_reply(binding, plan)
            return False
        return coverage_current(observed, datetime.now(timezone.utc),
                                hubspot_configured=coverage.hubspot_configured)

    def complete_verified(self, company, task_id, action_type, *, campaign_id, approval_id, campaign_version):
        """Attempt one Apollo-only completion after persisted receipt and binding.

        A POST is claimed durably beforehand and never retried after ambiguity.
        The caller holds the account lock for the entire dispatch/completion.
        """
        self._task_id(task_id)
        # A durable observation is not a capability to complete after restart.
        if self._coverage is None or self._safety is None:
            return {'state': 'apollo_reconcile_pending', 'reason': 'completion_live_check_required'}
        try:
            row, task, receipt_id = self._verified_binding(company, task_id, action_type, campaign_id, approval_id, campaign_version)
            if row['apollo_status'] != 'pending' or task.get('status') != 'scheduled':
                return {'state': 'apollo_reconcile_pending', 'reason': 'completion_unverified'}
            with self._repo.transaction():
                require_completion_guard(self._repo, company, task_id, action_type, receipt_id)
                approved = CampaignApprovalAuthority(self._repo).require(campaign_id, approval_id, campaign_version)
                if not self._execution_allowed() or not self._reply_clear(row, task, approved['binding']):
                    return {'state': 'apollo_reconcile_pending', 'reason': 'completion_live_check_blocked'}
                self._repo.claim_apollo_linkedin_completion(company, task_id, action_type)
            token = self._loader()
            if not isinstance(token, str) or not token or any(c.isspace() for c in token):
                raise ValueError('credential_unavailable')
            body = json.dumps({'note': 'Thunder OS: enviado via ConnectSafely, recibo ' + receipt_id}, ensure_ascii=False).encode()
            # Claim is already committed: a crash or unknown POST outcome must
            # never permit another POST. The separate fence serializes the last
            # pause/approval/reply observation with the network boundary.
            with self._repo.transaction():
                if not self._execution_allowed():
                    return {'state': 'apollo_reconcile_pending', 'reason': 'completion_paused'}
                approved = CampaignApprovalAuthority(self._repo).require(campaign_id, approval_id, campaign_version)
                if not self._reply_clear(row, task, approved['binding']) or not self._execution_allowed():
                    return {'state': 'apollo_reconcile_pending', 'reason': 'completion_live_check_blocked'}
                self._post_request('POST', '/api/v1/tasks/' + task_id + '/complete', body,
                                   {'x-api-key': token, 'Accept': 'application/json', 'Content-Type': 'application/json'}, self.timeout)
            _, readback, _ = self._verified_binding(company, task_id, action_type, campaign_id, approval_id, campaign_version)
            if (readback.get('status') != 'completed' or readback.get('note') !=
                    'Thunder OS: enviado via ConnectSafely, recibo ' + receipt_id):
                return {'state': 'apollo_reconcile_pending', 'reason': 'completion_unverified'}
            self._repo.reconcile_apollo_linkedin(company, task_id, action_type)
            return {'state': 'confirmed', 'apollo_status': 'confirmed'}
        except Exception:
            return {'state': 'apollo_reconcile_pending', 'reason': 'completion_unverified'}

    def reconcile_verified(self, company, task_id, action_type, *, campaign_id, approval_id, campaign_version):
        """Observe only Apollo after an attempted completion; never retry POST."""
        self._task_id(task_id)
        try:
            row, task, receipt_id = self._verified_binding(company, task_id, action_type, campaign_id, approval_id, campaign_version, historical=True)
            if (row['apollo_status'] != 'attempted' or task.get('status') != 'completed'
                    or task.get('note') != 'Thunder OS: enviado via ConnectSafely, recibo ' + receipt_id):
                raise ValueError('completion_unverified')
            self._repo.reconcile_apollo_linkedin(company, task_id, action_type)
            return {'state': 'confirmed', 'apollo_status': 'confirmed'}
        except Exception:
            return {'state': 'apollo_reconcile_pending', 'reason': 'completion_unverified'}
