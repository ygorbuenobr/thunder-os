"""Read-only Apollo task search and explicit routing for LinkedIn work."""
from __future__ import annotations

import re
import http.client
import json
import math
import os
import ssl
from urllib.parse import urlencode

from .apollo_linkedin_preview import _json

_ID = re.compile(r'[A-Za-z0-9_.-]{1,160}\Z')


def _valid(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


class CampaignBindings:
    """Immutable explicit company/campaign/account/credential associations.

    The credential is a reference for the consumer, never a credential value.
    The calling service must resolve it from its trusted configuration.
    """
    def __init__(self, bindings):
        if not isinstance(bindings, list) or not bindings:
            raise ValueError('bindings_invalid')
        self._routes = {}
        for binding in bindings:
            if not isinstance(binding, dict):
                raise ValueError('bindings_invalid')
            fields = ('company_id', 'team_id', 'campaign_id', 'account_id', 'credential_ref')
            if any(not _valid(binding.get(key)) for key in fields):
                raise ValueError('bindings_invalid')
            owners = binding.get('owner_ids')
            if (not isinstance(owners, list) or not owners or
                    any(not _valid(owner) for owner in owners) or len(set(owners)) != len(owners)):
                raise ValueError('bindings_invalid')
            key = (binding['company_id'], binding['team_id'], binding['campaign_id'])
            if key in self._routes:
                raise ValueError('binding_ambiguous')
            self._routes[key] = {field: binding[field] for field in fields}
            self._routes[key]['owner_ids'] = tuple(owners)

    def resolve(self, *, company_id, team_id, campaign_id, owner_id, account_status_reader):
        binding = self._routes.get((company_id, team_id, campaign_id))
        if binding is None:
            return {'status': 'blocked', 'reason': 'campaign_binding_missing'}
        if owner_id not in binding['owner_ids']:
            return {'status': 'blocked', 'reason': 'owner_not_configured'}
        if not callable(account_status_reader):
            return {'status': 'blocked', 'reason': 'account_status_unavailable'}
        try:
            account = account_status_reader(binding['account_id'])
        except Exception:
            account = None
        if not isinstance(account, dict) or account.get('id') != binding['account_id'] or account.get('available') is not True:
            return {'status': 'blocked', 'reason': 'default_account_unavailable',
                    'account_id': binding['account_id']}
        return {'status': 'ready', 'reason': None, 'account_id': binding['account_id'],
                'credential_ref': binding['credential_ref']}



_SEARCH = '/api/v1/tasks/search'
_ACTIONS = {'linkedin_step_connect', 'linkedin_step_message'}


def _https_request(method, path, body, headers, timeout):
    # The search endpoint is a read, despite using POST. No other mutation
    # method or endpoint reaches the network, even with a crafted query/path.
    if method == 'POST' and path == _SEARCH:
        if (not isinstance(body, dict) or set(body) != {'page', 'per_page'}
                or type(body['page']) is not int or not 1 <= body['page'] <= 500
                or body['per_page'] != 100):
            raise ValueError('endpoint_blocked')
        request_path = path + '?' + urlencode(body)
    elif (method == 'GET' and body is None and
          (path == '/api/v1/users/api_profile' or
           re.fullmatch(r'/api/v1/tasks/[A-Za-z0-9_-]{1,128}', path))):
        request_path = path
    else:
        raise ValueError('endpoint_blocked')
    if any(os.environ.get(key) for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR')):
        raise ValueError('tls_override_blocked')
    connection = http.client.HTTPSConnection('api.apollo.io', 443, timeout=timeout,
                                             context=ssl.create_default_context())
    try:
        connection.request(method, request_path, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError('response_too_large')
        return response.status, raw
    finally:
        connection.close()


class ApolloLinkedInReader:
    """Bounded team search. Empty means zero only with proven complete pagination."""
    def __init__(self, credential_loader, *, team_id, api_actor_id, owner_ids,
                 request=None, timeout=8, max_pages=500):
        if (not callable(credential_loader) or not _valid(team_id) or not _valid(api_actor_id)
                or not isinstance(owner_ids, (tuple, list)) or not owner_ids
                or any(not _valid(x) for x in owner_ids) or len(set(owner_ids)) != len(owner_ids)
                or type(max_pages) is not int or not 1 <= max_pages <= 500
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 15):
            raise ValueError('configuration_invalid')
        self._loader, self._request = credential_loader, request or _https_request
        self.team_id, self.api_actor_id, self.owner_ids = team_id, api_actor_id, frozenset(owner_ids)
        self.timeout, self.max_pages = timeout, max_pages

    def _call(self, method, path, body=None):
        # Enforce the allowlist even if a request transport is injected.
        allowed = (method == 'POST' and path == _SEARCH and isinstance(body, dict)
                   and set(body) == {'page', 'per_page'} and type(body['page']) is int
                   and 1 <= body['page'] <= 500 and body['per_page'] == 100)
        allowed = allowed or (method == 'GET' and body is None and
                              (path == '/api/v1/users/api_profile' or
                               re.fullmatch(r'/api/v1/tasks/[A-Za-z0-9_-]{1,128}', path)))
        if not allowed:
            raise ValueError('endpoint_blocked')
        token = self._loader()
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError('credential_unavailable')
        status, raw = self._request(method, path, body,
                                    {'x-api-key': token, 'Accept': 'application/json'}, self.timeout)
        if status != 200:
            raise ValueError('apollo_unavailable')
        return _json(raw)

    def _verify_team(self):
        result = self._call('GET', '/api/v1/users/api_profile')
        user = result.get('user', result)
        if not isinstance(user, dict) or user.get('id') != self.api_actor_id or user.get('team_id') != self.team_id:
            raise ValueError('team_mismatch')

    def get(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
            raise ValueError('invalid_task_id')
        self._verify_team()
        task = self._call('GET', '/api/v1/tasks/' + task_id).get('task')
        if not isinstance(task, dict) or task.get('id') != task_id:
            raise ValueError('task_mismatch')
        return task

    def list_open_tasks(self):
        tasks, seen = [], set()
        try:
            self._verify_team()
            expected = None
            for page in range(1, self.max_pages + 1):
                data = self._call('POST', _SEARCH, {'page': page, 'per_page': 100})
                rows, pagination = data.get('tasks'), data.get('pagination')
                if not isinstance(rows, list) or not isinstance(pagination, dict):
                    raise ValueError('pagination_incomplete')
                p, size, total, pages = (pagination.get(k) for k in ('page', 'per_page', 'total_entries', 'total_pages'))
                if (any(type(x) is not int for x in (p, size, total, pages)) or p != page
                        or size != 100 or total < 0 or total >= 50000 or pages != (total + 99) // 100
                        or pages > self.max_pages or len(rows) != min(100, max(0, total - (page-1)*100))):
                    raise ValueError('pagination_incomplete')
                if expected is not None and expected != (total, pages):
                    raise ValueError('pagination_incomplete')
                expected = total, pages
                for row in rows:
                    if (not isinstance(row, dict) or not _valid(row.get('id')) or row['id'] in seen
                            or not isinstance(row.get('type'), str) or not isinstance(row.get('status'), str)):
                        raise ValueError('pagination_incomplete')
                    seen.add(row['id'])
                    if row.get('status') == 'scheduled' and row.get('type') in _ACTIONS:
                        # A missing owner is retained so the executor can visibly block it.
                        if row.get('user_id') in self.owner_ids or not row.get('user_id'):
                            tasks.append(row)
                if page >= pages:
                    return {'tasks': tasks, 'coverage': 'complete', 'reason': None}
            raise ValueError('pagination_incomplete')
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                'pagination_incomplete', 'team_mismatch', 'credential_unavailable', 'apollo_unavailable'
            } else 'apollo_read_unavailable'
            return {'tasks': tasks, 'coverage': 'unknown', 'reason': reason}


def list_open_tasks(reader=None):
    if reader is None:
        return {'tasks': [], 'coverage': 'unknown', 'reason': 'apollo_get_task_list_unavailable'}
    return reader.list_open_tasks()
