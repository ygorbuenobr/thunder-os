"""Instantly custom-header webhook preparation, OFF before any side effect."""
from collections.abc import Mapping
from copy import deepcopy
import hmac
import http.client
import json
import math
import os
import re
import ssl

from .external_email_import import (INSTANTLY_CAMPAIGN, INSTANTLY_WORKSPACE,
    ExternalEmailError, email, require, timestamp)
from .models import payload_hash

HEADER_NAME = 'X-Thunder-Instantly-Secret'
EVENT_TYPES = ('email_sent', 'reply_received', 'auto_reply_received')


def contract_status():
    return {'provider': 'instantly', 'campaign_id': 'BR-INSTANTLY-01',
            'external_campaign_id': INSTANTLY_CAMPAIGN, 'account_id': INSTANTLY_WORKSPACE,
            'api_version': 'v2', 'enabled': False, 'writer_enabled': False,
            'status': 'configuration_pending', 'configuration_pending': True,
            'provider_verified': False, 'email_logged': False, 'webhook_registered': False,
            'delivery_auth': 'shared_custom_header', 'required_scopes': ['workspaces:read', 'emails:read']}


def _uuid(value):
    return isinstance(value, str) and bool(re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value))


def _payload(value):
    require(isinstance(value, Mapping), 'invalid_payload')
    require(value.get('workspace') == INSTANTLY_WORKSPACE
            and value.get('campaign_id') == INSTANTLY_CAMPAIGN, 'instantly_identity_mismatch')
    require(value.get('event_type') in EVENT_TYPES, 'unsupported_event_type')
    require(_uuid(value.get('email_id')), 'missing_or_invalid_email_id')
    email(value.get('lead_email'))
    email(value.get('email_account'))
    timestamp(value.get('timestamp'))
    return value


def dry_run(payload=None):
    """Accept a private payload for shape checks; it remains untrusted input.

    No fixture identity/content is returned, retained or passed to any provider.
    Authentication belongs to the enclosing human simulation route.
    """
    sample = {'workspace': INSTANTLY_WORKSPACE, 'campaign_id': INSTANTLY_CAMPAIGN,
              'campaign_name': 'Synthetic Pavlack', 'event_type': 'email_sent',
              'timestamp': '2026-10-21T12:00:00Z',
              'email_id': '00000000-0000-4000-8000-000000000001',
              'lead_email': 'pavlack@example.invalid', 'email_account': 'sender@example.invalid',
              'email_text': 'Synthetic fixture only.'}
    value = sample if payload is None else payload
    result = {'structurally_valid': True}
    try:
        _payload(value)
    except (ExternalEmailError, TypeError, KeyError) as exc:
        result = {'structurally_valid': False,
                  'error_code': exc.code if isinstance(exc, ExternalEmailError) else 'invalid_payload'}
    return {**result, 'status': 'structural_dry_run', 'simulation': True,
            'needs_provider_readback': True, 'provider_verified': False,
            'email_logged': False, 'writes': 0, 'db_writes': 0, 'persisted': False}


def _https_request(method, path, body, headers, timeout):
    require(not any(os.environ.get(key) for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR')),
            'tls_override_blocked')
    connection = http.client.HTTPSConnection('api.instantly.ai', 443, timeout=timeout,
                                            context=ssl.create_default_context())
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        require(len(raw) <= 1_000_000, 'response_too_large')
        return response.status, raw
    finally:
        connection.close()


class InstantlyReadOnly:
    def __init__(self, credential_loader=None, *, request=None, timeout=15):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30,
                'invalid_timeout')
        self._loader, self._request, self.timeout = credential_loader, request or _https_request, timeout

    def _get(self, path):
        require(path == '/api/v2/workspaces/current' or bool(re.fullmatch(
            r'/api/v2/emails/[0-9a-f-]{36}', path)), 'instantly_endpoint_blocked')
        require(callable(self._loader), 'configuration_pending')
        try:
            token = self._loader()
            require(isinstance(token, str) and token and not any(c.isspace() for c in token),
                    'credential_unavailable')
            status, raw = self._request('GET', path, None,
                {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, self.timeout)
            require(type(status) is int and 200 <= status < 300, 'instantly_http_error')
            require(isinstance(raw, bytes) and len(raw) <= 1_000_000, 'invalid_response')
            result = json.loads(raw)
            require(isinstance(result, dict), 'invalid_response')
            return result
        except ExternalEmailError:
            raise
        except Exception:
            raise ExternalEmailError('instantly_read_unavailable') from None

    def read_event(self, payload):
        value = _payload(payload)
        # Workspace comes from the actual bearer key, never a caller's assertion.
        workspace = self._get('/api/v2/workspaces/current')
        require(workspace.get('id') == INSTANTLY_WORKSPACE, 'instantly_workspace_mismatch')
        row = self._get('/api/v2/emails/' + value['email_id'])
        return _canonical_event(value, row)


def _canonical_event(value, row):
    require(isinstance(row, Mapping) and row.get('id') == value['email_id']
            and row.get('organization_id') == INSTANTLY_WORKSPACE
            and row.get('campaign_id') == INSTANTLY_CAMPAIGN, 'instantly_readback_mismatch')
    lead, account = email(value['lead_email']), email(value['email_account'])
    require(email(row.get('lead')) == lead and email(row.get('eaccount')) == account,
            'instantly_recipient_mismatch')
    kind = value['event_type']
    ue_type = row.get('ue_type')
    require(type(ue_type) is int, 'instantly_email_type_missing')
    if kind == 'email_sent':
        require(ue_type in (1, 3), 'instantly_email_type_mismatch')
        recipients = row.get('to_address_email_list')
        require(isinstance(recipients, str) and lead in {email(item) for item in recipients.split(',')},
                'instantly_recipient_mismatch')
        require(email(row.get('from_address_email')) == account, 'instantly_sender_mismatch')
        human = False
    else:
        require(ue_type == 2 and email(row.get('from_address_email')) == lead,
                'instantly_email_type_mismatch')
        recipients = row.get('to_address_email_list')
        require(isinstance(recipients, str) and account in {email(item) for item in recipients.split(',')},
                'instantly_recipient_mismatch')
        auto = row.get('is_auto_reply')
        human = kind == 'reply_received' and type(auto) is int and auto == 0
        if type(auto) is int and auto == 1:
            kind = 'auto_reply_received'
    body = row.get('body')
    require(isinstance(body, Mapping) and isinstance(body.get('text'), str)
            and len(body['text']) <= 200_000 and isinstance(row.get('subject'), str)
            and len(row['subject']) <= 1000, 'invalid_email_content')
    source = {'provider': 'instantly', 'account_id': INSTANTLY_WORKSPACE,
              'external_campaign_id': INSTANTLY_CAMPAIGN, 'campaign_id': 'BR-INSTANTLY-01',
              'provider_id': row['id']}
    event = {'source': source, 'event_type': kind, 'lead_email': lead,
             'occurred_at': timestamp(row.get('timestamp_email')),
             'subject': row['subject'], 'text': body['text'], 'human_reply': human}
    event['effect_key'] = payload_hash({'provider': 'instantly', 'account_id': INSTANTLY_WORKSPACE,
                                      'provider_id': row['id']})
    return event


class InstantlyWebhook:
    def __init__(self, secret_loader=None, *, provider=None, enabled=False, header_name=HEADER_NAME):
        require(isinstance(header_name, str) and bool(re.fullmatch(r'[A-Za-z0-9-]{1,100}', header_name)),
                'invalid_header_name')
        require(header_name.lower() not in ('authorization', 'cookie', 'host'), 'invalid_header_name')
        self._secret, self.provider, self.enabled = secret_loader, provider, enabled
        self.header_name = header_name

    def handle(self, headers, payload):
        if self.enabled is not True:
            return {'status': 'disabled', 'http_status': 410, 'writes': 0,
                    'provider_verified': False, 'email_logged': False, 'persisted': False}
        if not callable(self._secret):
            return self._pending('configuration_pending')
        try:
            secret = self._secret()
        except Exception:
            return self._pending('configuration_pending')
        if not isinstance(secret, str) or not secret or len(secret) > 4096:
            return self._pending('configuration_pending')
        values = [value for key, value in headers.items() if isinstance(key, str)
                  and key.lower() == self.header_name.lower()] if isinstance(headers, Mapping) else []
        valid = len(values) == 1 and isinstance(values[0], str) and len(values[0]) <= 4096
        if not valid or not hmac.compare_digest(values[0].encode(), secret.encode()):
            return {'status': 'unauthorized', 'http_status': 401, 'writes': 0,
                    'provider_verified': False, 'email_logged': False, 'persisted': False}
        try:
            value = deepcopy(_payload(payload))
        except (ExternalEmailError, TypeError, KeyError):
            return {'status': 'invalid_payload', 'http_status': 422, 'writes': 0,
                    'provider_verified': False, 'email_logged': False, 'persisted': False}
        if self.provider is None:
            return self._pending('configuration_pending')
        try:
            event = self.provider.read_event(value)
            # The injected port is trusted code, not a client-supplied verified flag.
            require(isinstance(event, Mapping) and event.get('source', {}).get('provider_id') == value['email_id']
                    and event.get('source', {}).get('account_id') == INSTANTLY_WORKSPACE
                    and event.get('source', {}).get('external_campaign_id') == INSTANTLY_CAMPAIGN
                    and event.get('lead_email') == email(value['lead_email']), 'instantly_readback_mismatch')
        except Exception:
            return self._pending('provider_readback_pending')
        return {'status': 'prepared', 'http_status': 200, 'event': event,
                'provider_verified': True, 'email_logged': False, 'persisted': False,
                'writes': 0, 'writer_enabled': False, 'external_platforms_stopped': False}

    @staticmethod
    def _pending(code):
        return {'status': code, 'http_status': 503, 'provider_verified': False,
                'email_logged': False, 'writes': 0, 'persisted': False}
