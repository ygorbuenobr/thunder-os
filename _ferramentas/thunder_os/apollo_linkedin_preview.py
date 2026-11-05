"""Bounded Apollo task read and non-executing LinkedIn preview.

Only an exact task ID and its Apollo team are accepted from trusted server
configuration. No provider write, queue, storage or dispatch capability exists
in this module. Returned data is a read projection, not an authorization.
"""
from __future__ import annotations

import http.client
import hashlib
import json
import math
import os
import re
import ssl
from urllib.parse import urlsplit

_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_SLUG = re.compile(r'[A-Za-z0-9_.-]{1,160}\Z')


def validated_task_text(task, action, *, invite_limit=300, message_limit=1000):
    """Validate and hash the exact per-contact Apollo body without rewriting it."""
    if any(type(limit) is not int or not 1 <= limit <= 100_000
           for limit in (invite_limit, message_limit)):
        raise ValueError('configuration_invalid')
    template = task.get('linkedin_emailer_template') if isinstance(task, dict) else None
    text = template.get('body_text') if isinstance(template, dict) else None
    if not isinstance(text, str):
        return None, None, 'text_missing'
    if not text.strip():
        return None, None, 'text_empty'
    if '{{' in text or '}}' in text:
        return None, None, 'text_unresolved_variables'
    if action not in ('invite', 'message'):
        return None, None, 'unsupported_action'
    if len(text) > (invite_limit if action == 'invite' else message_limit):
        return None, None, 'text_too_long'
    return text, hashlib.sha256(text.encode('utf-8')).hexdigest(), None


def _identifier(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _slug(url):
    if not isinstance(url, str) or len(url) > 500:
        return None
    parsed = urlsplit(url)
    if (parsed.scheme, parsed.netloc.lower()) not in (
            ('https', 'www.linkedin.com'), ('https', 'linkedin.com'),
            ('http', 'www.linkedin.com'), ('http', 'linkedin.com')):
        return None
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        return None
    parts = parsed.path.strip('/').split('/')
    return parts[1].lower() if len(parts) == 2 and parts[0] == 'in' and _SLUG.fullmatch(parts[1]) else None


def _https_request(method, path, body, headers, timeout):
    if (method != 'GET' or body is not None or not
            (path == '/api/v1/users/api_profile' or re.fullmatch(r'/api/v1/tasks/[A-Za-z0-9_-]{1,128}', path))):
        raise ValueError('endpoint_blocked')
    if any(os.environ.get(key) for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR')):
        raise ValueError('tls_override_blocked')
    connection = http.client.HTTPSConnection('api.apollo.io', 443, timeout=timeout,
                                             context=ssl.create_default_context())
    try:
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError('response_too_large')
        return response.status, raw
    finally:
        connection.close()


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    if not isinstance(raw, bytes) or len(raw) > 1_000_000:
        raise ValueError('invalid_response')
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_number')))
    if not isinstance(value, dict):
        raise ValueError('invalid_response')
    return value


class ApolloLinkedInPreview:
    def __init__(self, credential_loader, *, team_id, sequence_id, task_id,
                 account_id, account_public_id, api_actor_id, owner_id, account_status_reader,
                 request=None, timeout=8, invite_text_limit=300, message_text_limit=1000):
        if not all(_identifier(x) for x in (team_id, sequence_id, task_id, account_id, api_actor_id, owner_id)):
            raise ValueError('configuration_invalid')
        if not isinstance(account_public_id, str) or not _SLUG.fullmatch(account_public_id):
            raise ValueError('configuration_invalid')
        if not callable(credential_loader) or not callable(account_status_reader):
            raise ValueError('configuration_invalid')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 15:
            raise ValueError('configuration_invalid')
        if any(type(limit) is not int or not 1 <= limit <= 100_000
               for limit in (invite_text_limit, message_text_limit)):
            raise ValueError('configuration_invalid')
        self._loader, self._request = credential_loader, request or _https_request
        self._status = account_status_reader
        self.team_id, self.sequence_id, self.task_id = team_id, sequence_id, task_id
        self.account_id, self.account_public_id, self.owner_id = account_id, account_public_id, owner_id
        self.api_actor_id = api_actor_id
        self.timeout = timeout
        self.invite_text_limit, self.message_text_limit = invite_text_limit, message_text_limit

    def _get(self, path):
        if path not in ('/api/v1/users/api_profile', '/api/v1/tasks/' + self.task_id):
            raise ValueError('endpoint_blocked')
        token = self._loader()
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError('credential_unavailable')
        status, raw = self._request('GET', path, None,
            {'x-api-key': token, 'Accept': 'application/json'}, self.timeout)
        if status in (404, 422) and path == '/api/v1/tasks/' + self.task_id:
            raise ValueError('task_removed')
        if status != 200:
            raise ValueError('apollo_unavailable')
        return _json(raw)

    def read(self):
        result = {'status': 'unavailable', 'execution_enabled': False, 'dry_run': True,
                  'task': None, 'account': None, 'lead': None, 'action': None,
                  'text': {'value': None, 'verified': False}, 'blockers': [], 'call_preview': None}
        try:
            profile = self._get('/api/v1/users/api_profile')
            user = profile.get('user', profile)
            if (not isinstance(user, dict) or user.get('team_id') != self.team_id
                    or user.get('id') != self.api_actor_id):
                raise ValueError('team_mismatch')
            data = self._get('/api/v1/tasks/' + self.task_id)
            task = data.get('task')
            if not isinstance(task, dict) or task.get('id') != self.task_id:
                raise ValueError('task_mismatch')
            contact = task.get('contact')
            if task.get('emailer_campaign_id') != self.sequence_id:
                raise ValueError('sequence_unverified')
            if (not isinstance(contact, dict) or not _identifier(contact.get('id'))
                    or task.get('contact_id') != contact['id']):
                raise ValueError('contact_unverified')
            result['task'] = {'id': self.task_id, 'status': task.get('status'),
                              'sequence_id': self.sequence_id,
                              'step_position': None}
            name = contact.get('name')
            profile_slug = _slug(contact.get('linkedin_url'))
            result['lead'] = {'id': contact['id'], 'name': name if isinstance(name, str) and len(name) <= 300 else None,
                              'linkedin_url': 'https://www.linkedin.com/in/' + profile_slug + '/' if profile_slug else None}
            result['action'] = {'linkedin_step_connect': 'invite', 'linkedin_step_message': 'message'}.get(task.get('type'))
            blockers = result['blockers']
            if task.get('status') != 'scheduled': blockers.append('task_not_open')
            if result['action'] is None: blockers.append('unsupported_action')
            if task.get('user_id') != self.owner_id: blockers.append('owner_unverified')
            recipient = _slug(contact.get('linkedin_url'))
            if not recipient: blockers.append('profile_unverified')
            elif recipient == self.account_public_id.lower(): blockers.append('self_invitation' if result['action'] == 'invite' else 'self_message')
            text, text_hash, text_blocker = validated_task_text(
                task, result['action'], invite_limit=self.invite_text_limit,
                message_limit=self.message_text_limit)
            result['text'] = {'value': text, 'verified': text_blocker is None, 'hash': text_hash}
            if text_blocker and text_blocker not in blockers:
                blockers.append(text_blocker)
            try:
                account = self._status()
            except Exception:
                account = None
            verified = (isinstance(account, dict) and account.get('id') == self.account_id
                        and account.get('publicId') == self.account_public_id and account.get('available') is True)
            result['account'] = {'id': self.account_id, 'public_id': self.account_public_id, 'verified': verified}
            if not verified: blockers.append('account_unverified')
            # This is a non-executable shape, never a request. Unknown
            # recipient URN and exact content remain null; do not infer them
            # from Apollo notes or from a LinkedIn public URL.
            if (verified and recipient and recipient != self.account_public_id.lower()
                    and result['action'] is not None and task.get('status') == 'scheduled'
                    and task.get('user_id') == self.owner_id):
                if result['action'] == 'invite':
                    path = '/linkedin/connect'
                    body = {'accountId': self.account_id, 'profileId': recipient,
                            'profileUrn': None, 'customMessage': text}
                else:
                    path = '/linkedin/conversations/send'
                    body = {'accountId': self.account_id, 'recipientProfileId': recipient,
                            'recipientProfileUrn': None, 'messagingChannel': 'linkedin_inbox',
                            'message': text}
                result['call_preview'] = {'method': 'POST', 'path': path, 'body': body,
                                          'incomplete': True, 'executable': False}
            # Provider recipient URN, approval and reply coverage are not
            # established by this read. Never build an executable payload.
            blockers.extend(['recipient_urn_unverified', 'approval_unverified', 'reply_coverage_unverified'])
            result['status'] = 'blocked'
            return result
        except Exception as exc:
            reason = 'task_removed' if isinstance(exc, ValueError) and str(exc) == 'task_removed' else 'read_unavailable'
            result.update(status='unavailable', task=None, account=None, lead=None, action=None,
                          text={'value': None, 'verified': False}, blockers=[reason], call_preview=None)
            return result
