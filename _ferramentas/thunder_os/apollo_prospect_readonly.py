"""Bounded Apollo v1 free reads; no enrichment, registration or execution ports.

Docs checked 2026-10-23: docs.apollo.io/reference/{people-api-search,
search-for-contacts,search-for-sequences,view-credit-usage-stats,get-current-user-profile}.
Each public call makes at most one request, with no automatic retries. Search
``complete`` means the entire matching set is present in this envelope, not just
that a page was received. Use source.has_more/next_page for explicit paging.
Injected transports must honor the same timeout, byte and no-redirect contract
as apollo_readonly._https_request; they are trusted application dependencies.
"""
from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import math
import re
import time
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from .apollo_readonly import _https_request
from .external_email_import import ExternalEmailError, require


_PREFIX = '/api/v1/'
_OPERATIONS = {
    'people_search': ('mixed_people/api_search', 'people', 'mixed_people_api_search'),
    'contacts_search': ('contacts/search', 'contacts', 'contacts_search'),
    'sequences_search': ('emailer_campaigns/search', 'emailer_campaigns', 'emailer_campaigns_search'),
    'credit_usage': ('usage_stats/credit_usage_stats', None, 'credit_usage_stats_read'),
    'profile': ('users/api_profile', None, 'read_user_profile'),
}
_FILTERS = frozenset({'person_titles', 'person_locations', 'organization_locations',
                      'organization_num_employees_ranges', 'q_keywords'})
_CREDIT_TYPES = frozenset({'lead_credit', 'direct_dial_credit', 'export_credit',
    'conversation_credit', 'ai_credit', 'power_up_credit', 'inbound_website_visitor_credit',
    'contact_website_visitor_credit', 'web_search_record_credit', 'dialer'})
_MAX_BYTES = 1_000_000
_IDENTITY_TTL = 300
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_RANGE = re.compile(r'([0-9]{1,9}),([0-9]{1,9})\Z')


def _text(value, token='', limit=500):
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or token and token in value):
        return None
    return value


def _number(value):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
        value = int(value)
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _strict_id(value, token):
    value = _text(value, token, 128)
    return value if value is not None and _ID.fullmatch(value) else None


def _linkedin(value, token):
    value = _text(value, token)
    if value is None:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == 'https' and url.netloc in ('linkedin.com', 'www.linkedin.com')
                and not url.query and not url.fragment
                and re.fullmatch(r'/in/[A-Za-z0-9][A-Za-z0-9_.-]{0,199}/?', url.path)):
            return value
    except ValueError:
        pass
    return None


def _campaign_statuses(rows, token):
    """Preserve literal provider values; incomplete memberships cannot prove activation."""
    if not isinstance(rows, list) or len(rows) > 50:
        return [], False
    items, seen, duplicates, complete = [], set(), set(), True
    ids = {'id', 'send_email_from_user_id', 'added_by_user_id',
           'send_email_from_email_account_id', 'current_step_id',
           'in_response_to_emailer_message_id'}
    nullable = {'current_step_id', 'in_response_to_emailer_message_id', 'inactive_reason',
                'failure_reason', 'finished_at', 'paused_at', 'auto_unpause_at',
                'manually_set_unpause', 'current_step_position'}
    dates = {'added_at', 'finished_at', 'paused_at', 'auto_unpause_at'}
    fields = ids | dates | {'status', 'state', 'inactive_reason', 'failure_reason',
                            'manually_set_unpause', 'current_step_position'}
    for row in rows:
        if not isinstance(row, dict):
            complete = False
            continue
        campaign = _strict_id(row.get('emailer_campaign_id'), token)
        if campaign is None:
            complete = False
            continue
        if campaign in seen:
            duplicates.add(campaign)
            complete = False
        seen.add(campaign)
        try:
            status = _text(row.get('status'), token, 80)
            require(status is not None and status.strip(), 'invalid_membership')
            item = {'emailer_campaign_id': campaign}
            for key in fields & row.keys():
                value = row[key]
                if value is None and key in nullable:
                    item[key] = None
                    continue
                if key in ids:
                    require(_strict_id(value, token) is not None, 'invalid_membership')
                elif key in dates:
                    require(_text(value, token, 64) is not None and 'T' in value,
                            'invalid_membership')
                    try:
                        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                        require(parsed.utcoffset() is not None, 'invalid_membership')
                    except ValueError:
                        raise ExternalEmailError('invalid_membership') from None
                elif key == 'manually_set_unpause':
                    require(type(value) is bool, 'invalid_membership')
                elif key == 'current_step_position':
                    require(type(value) is int and 1 <= value <= 10000, 'invalid_membership')
                else:
                    require(_text(value, token) is not None and value.strip(), 'invalid_membership')
                item[key] = value
            if status not in ('active', 'paused', 'failed', 'finished'):
                complete = False
            items.append(item)
        except ExternalEmailError:
            complete = False
    return [item for item in items if item['emailer_campaign_id'] not in duplicates], complete


def _observe_team(data, rows, source, token):
    """Only explicit same-key profile proof can verify observed or absent team fields."""
    verified_profile = source.get('identity_source') == 'verified_profile' and source.get('identity_verified') is True
    teams, contact_team, invalid = set(), False, False
    for row in [data, *rows]:
        if not isinstance(row, dict) or 'team_id' not in row:
            continue
        team = _strict_id(row['team_id'], token)
        if team is None:
            invalid = True
            continue
        teams.add(team)
        contact_team |= row is not data
    profile_team = source.get('account_id') if verified_profile else None
    if not verified_profile:
        source['account_id'] = None
        source['identity_verified'] = False
    if teams:
        source['observed_team_ids'] = sorted(teams)
    if invalid or len(teams) != 1:
        if not teams and not invalid and verified_profile:
            return False
        source.update(account_id=None, identity_verified=False)
        source.pop('authenticated_user_id', None)
        if verified_profile:
            source['identity_source'] = 'profile_team_conflict'
            source.pop('team_id', None)
            source.pop('identity_cache_age_seconds', None)
        source['identity_reason'] = ('provider_team_invalid' if invalid else
                                     'provider_team_mixed' if teams else 'provider_team_unavailable')
        return invalid or bool(teams)
    team = next(iter(teams))
    if verified_profile:
        if team != profile_team:
            source.update(account_id=None, identity_verified=False,
                          identity_source='profile_team_conflict', identity_reason='profile_team_mismatch')
            source.pop('authenticated_user_id', None)
            source.pop('team_id', None)
            source.pop('identity_cache_age_seconds', None)
            return True
        source['team_id'] = team
        return False
    source.update(account_id=team, team_id=team,
                  identity_source='provider_contact_team' if contact_team else 'provider_response_team',
                  identity_reason='provider_team_observed_unverified')
    if source.get('injected_account_id') not in (None, team):
        source['identity_reason'] = 'injected_team_mismatch'
        return True
    return False


def _filters(filters):
    require(isinstance(filters, Mapping) and not set(filters) - _FILTERS, 'invalid_filters')
    result = {}
    for key, value in filters.items():
        if key == 'q_keywords':
            require(_text(value) is not None and bool(value.strip()), 'invalid_filters')
            result[key] = value
            continue
        require(isinstance(value, list) and 1 <= len(value) <= 50, 'invalid_filters')
        require(all(_text(item, limit=200) is not None and item.strip() for item in value),
                'invalid_filters')
        if key == 'organization_num_employees_ranges':
            for item in value:
                match = _RANGE.fullmatch(item)
                require(match is not None and int(match[1]) <= int(match[2]), 'invalid_filters')
        result[key] = list(value)
    return result


def _project(row, operation, token):
    require(isinstance(row, dict), 'invalid_item')
    ident = _text(row.get('id'), token, 128)
    require(ident is not None and _ID.fullmatch(ident), 'invalid_item')
    if operation == 'people_search':
        fields = ('first_name', 'last_name_obfuscated', 'title', 'last_refreshed_at')
    elif operation == 'contacts_search':
        fields = ('first_name', 'last_name', 'name', 'title', 'organization_name',
                  'organization_id', 'person_id', 'email_status', 'city', 'state', 'country')
    else:
        fields = ('name', 'user_id', 'created_at', 'last_used_at')
    result = {'id': ident}
    for key in fields:
        value = _text(row.get(key), token)
        if value is not None:
            result[key] = value
    if operation == 'people_search':
        for key in ('has_email', 'has_city', 'has_state', 'has_country'):
            if type(row.get(key)) is bool:
                result[key] = row[key]
        organization = row.get('organization')
        if isinstance(organization, dict):
            name = _text(organization.get('name'), token)
            if name is not None:
                result['organization'] = {'name': name}
    elif operation == 'contacts_search':
        address = _text(row.get('email'), token, 254)
        # Availability flags and masked addresses never become contact routes.
        if (address and re.fullmatch(r'[^@\s*<>]+@[^@\s*<>]+\.[^@\s*<>]+', address)
                and address.lower() != 'email_not_unlocked@domain.com'):
            result['email'] = address
        if type(row.get('email_unsubscribed')) is bool:
            result['email_unsubscribed'] = row['email_unsubscribed']
        team = _strict_id(row.get('team_id'), token)
        if team is not None:
            result['team_id'] = team
        linkedin = _linkedin(row.get('linkedin_url'), token)
        if linkedin is not None:
            result['linkedin_url'] = linkedin
    else:
        for key in ('active', 'archived'):
            if type(row.get(key)) is bool:
                result[key] = row[key]
        permission = row.get('sharing_permission')
        if isinstance(permission, dict):
            result['sharing_permission'] = {
                key: permission[key] for key, allowed in (
                    ('visibility', ('everyone', 'restricted')),
                    ('access_type', (None, 'can_view', 'can_edit', 'full_access')))
                if permission.get(key) in allowed and key in permission}
    return result


class ApolloProspectReadonly:
    """Use caller-owned auth; no credential discovery or hardcoded campaign/team.

    request(method, path, body, headers, timeout) -> (HTTP status, bytes), exactly
    as ApolloReadOnly. identity_reader, if supplied, returns {team_id: str}; this
    is injected context, never account_id or verified tenant identity. account_id
    requires observed team data or explicit profile() proof for the current key.
    profile() caches only user/team IDs, privately bound to a credential digest
    for 300 monotonic seconds on this instance. Calls are serialized by a lock;
    searches never auto-call profile. Verified identity is not namespace approval.
    Missing contact_campaign_statuses means unknown membership; a present list
    has its own contact_campaign_statuses_complete flag. max_rows is
    a search window (1..1000), not a cross-call usage counter. page is 1..500 and
    per_page is 1..100. Invalid caller input returns a redacted error envelope.
    """

    def __init__(self, credential_loader=None, *, identity_reader=None, request=None,
                 enabled=False, timeout=15, max_rows=1000):
        require(type(timeout) in (int, float) and math.isfinite(timeout)
                and 0 < timeout <= 30, 'invalid_timeout')
        require(type(max_rows) is int and 1 <= max_rows <= 1000, 'invalid_max_rows')
        self._loader, self._identity = credential_loader, identity_reader
        self._request = request or _https_request
        self.enabled, self.timeout, self.max_rows = enabled, timeout, max_rows
        self._identity_lock = threading.RLock()
        self._credential_fingerprint = None
        self._profile_identity = None

    def _source(self, operation, per_page=None):
        endpoint, _, scope = _OPERATIONS[operation]
        return {'provider': 'apollo', 'api_version': 'v1', 'operation': operation,
                'endpoint': _PREFIX + endpoint, 'required_scope': scope,
                'docs_checked_at': '2026-10-23', 'credit_cost_basis': 'official_documentation',
                'account_id': None, 'identity_source': 'unverified',
                'identity_verified': False, 'identity_reason': 'provider_team_unavailable',
                'environment': 'unverified', 'request_count': 0,
                'per_page': per_page, 'max_rows': self.max_rows,
                'has_more': False, 'next_page': None}

    @staticmethod
    def _envelope(source, page, *, items=None, total=None, reason=None, complete=False):
        return {'status': ('ok' if complete else 'partial') if reason in
                (None, 'more_pages', 'page_slice', 'row_limit', 'partial_response',
                 'pagination_unverified') else 'error',
                'items': items or [], 'total': total, 'page': page,
                'complete': complete, 'reason': reason, 'credit_cost': 0, 'source': source}

    def _call(self, method, path, body, source):
        with self._identity_lock:
            try:
                return self._call_locked(method, path, body, source)
            except Exception:
                self._profile_identity = None
                raise

    def _call_locked(self, method, path, body, source):
        """Guard the final wire request as well as public inputs, before auth."""
        require(_text(path, limit=50_000) is not None, 'endpoint_blocked')
        parsed = urlsplit(path)
        endpoint = parsed.path.removeprefix(_PREFIX)
        operation = next((key for key, value in _OPERATIONS.items() if value[0] == endpoint), None)
        require(operation is not None and method == ('GET' if operation == 'profile' else 'POST')
                and not parsed.scheme
                and not parsed.netloc and not parsed.fragment and parsed.path == _PREFIX + endpoint,
                'endpoint_blocked')
        try:
            params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True,
                              max_num_fields=205)
        except ValueError:
            raise ExternalEmailError('invalid_request') from None
        if operation in ('people_search', 'sequences_search'):
            require(body is None, 'invalid_request')
            allowed = ({key + '[]' if key != 'q_keywords' else key for key in _FILTERS}
                       if operation == 'people_search' else {'q_name'}) | {'page', 'per_page'}
            require(not set(params) - allowed and all(len(values) == 1 for key, values in
                    params.items() if not key.endswith('[]')), 'invalid_request')
            values = {key: vals if key.endswith('[]') else vals[0] for key, vals in params.items()}
            if operation == 'people_search':
                _filters({key.removesuffix('[]'): value for key, value in values.items()
                          if key not in ('page', 'per_page')})
            else:
                require(_text(values.get('q_name')) is not None, 'invalid_query')
        elif operation == 'contacts_search':
            require(not params and isinstance(body, dict)
                    and set(body) == {'q_keywords', 'page', 'per_page'}, 'invalid_request')
            require(_text(body['q_keywords']) is not None, 'invalid_query')
            values = body
        elif operation == 'profile':
            require(path == _PREFIX + 'users/api_profile' and body is None, 'invalid_request')
            values = None
        else:
            require(not params and body == {}, 'invalid_request')
            values = None
        if values is not None:
            page, per_page = _number(values.get('page')), _number(values.get('per_page'))
            require(page is not None and 1 <= page <= 500 and per_page is not None
                    and 1 <= per_page <= 100 and (page - 1) * per_page < self.max_rows,
                    'invalid_pagination')
        require(self.enabled is True, 'disabled')
        require(callable(self._loader), 'configuration_pending')
        try:
            token = self._loader()
        except Exception:
            raise ExternalEmailError('credential_unavailable') from None
        require(isinstance(token, str) and 0 < len(token) <= 4096 and token.isascii()
                and all(33 <= ord(c) <= 126 for c in token), 'credential_unavailable')
        fingerprint = hashlib.sha256(token.encode('ascii')).digest()
        if fingerprint != self._credential_fingerprint:
            self._profile_identity = None
            self._credential_fingerprint = fingerprint
        if operation == 'profile':
            self._profile_identity = None
        if self._identity is not None:
            try:
                identity = self._identity()
            except Exception:
                raise ExternalEmailError('identity_unavailable') from None
            team = _text(identity.get('team_id'), token, 128) if isinstance(identity, Mapping) else None
            require(team is not None and _ID.fullmatch(team), 'identity_unavailable')
            source.update(injected_account_id=team, identity_source='injected')
        source['request_count'] = 1
        started = time.monotonic()
        try:
            status, raw = self._request(method, path, body,
                {'x-api-key': token, 'Content-Type': 'application/json', 'Accept': 'application/json'},
                self.timeout)
        except TimeoutError:
            raise ExternalEmailError('timeout') from None
        except Exception:
            raise ExternalEmailError('provider_unavailable') from None
        require(time.monotonic() - started <= self.timeout, 'timeout')
        require(type(status) is int, 'invalid_response')
        reason = {401: 'credential_rejected', 403: 'permission_denied', 429: 'rate_limited'}.get(status)
        require(200 <= status < 300, reason or ('redirect_blocked' if 300 <= status < 400
                                              else 'provider_http_error'))
        require(isinstance(raw, bytes) and len(raw) <= _MAX_BYTES, 'invalid_response')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ExternalEmailError('invalid_response') from None
        require(isinstance(data, dict) and not any(key in data for key in
                ('error', 'errors', 'error_code', 'error_details')), 'invalid_response')
        now = time.monotonic()
        if operation == 'profile':
            user_id, team_id = _strict_id(data.get('id'), token), _strict_id(data.get('team_id'), token)
            require(user_id is not None and team_id is not None, 'invalid_profile')
            self._profile_identity = (fingerprint, now, user_id, team_id)
        cached = self._profile_identity
        if cached is not None:
            digest, observed_at, user_id, team_id = cached
            age = now - observed_at
            if digest == fingerprint and 0 <= age < _IDENTITY_TTL:
                source.update(account_id=team_id, team_id=team_id, authenticated_user_id=user_id,
                              identity_source='verified_profile', identity_verified=True,
                              identity_reason='profile_identity_observed',
                              identity_cache_age_seconds=int(age), identity_ttl_seconds=_IDENTITY_TTL)
            else:
                self._profile_identity = None
        return data, token

    def _observe_identity(self, data, rows, source, token):
        invalid = _observe_team(data, rows, source, token)
        if source.get('identity_source') == 'profile_team_conflict':
            with self._identity_lock:
                if self._credential_fingerprint == hashlib.sha256(token.encode('ascii')).digest():
                    self._profile_identity = None
        return invalid

    def _search(self, operation, query, page, per_page):
        source = self._source(operation, per_page if type(per_page) is int and 1 <= per_page <= 100 else None)
        safe_page = page if type(page) is int and 1 <= page <= 500 else None
        try:
            require(type(page) is int and 1 <= page <= 500 and type(per_page) is int
                    and 1 <= per_page <= 100 and (page - 1) * per_page < self.max_rows,
                    'invalid_pagination')
            if operation == 'people_search':
                params = {key + '[]' if key != 'q_keywords' else key: value
                          for key, value in _filters(query).items()}
            else:
                require(_text(query) is not None, 'invalid_query')
                params = {'q_keywords' if operation == 'contacts_search' else 'q_name': query}
            params.update(page=page, per_page=per_page)
            path = source['endpoint']
            body = params if operation == 'contacts_search' else None
            if body is None:
                path += '?' + urlencode(params, doseq=True)
            data, token = self._call('POST', path, body, source)
            rows = data.get(_OPERATIONS[operation][1])
            require(isinstance(rows, list) and len(rows) <= per_page, 'invalid_response')
            pagination = data.get('pagination')
            if operation == 'people_search' and pagination is None:
                total = _number(data.get('total_entries'))
                metadata_valid = total is not None
            else:
                pagination = pagination if isinstance(pagination, dict) else {}
                total = _number(pagination.get('total_entries'))
                metadata_valid = (total is not None and _number(pagination.get('page')) == page
                                  and _number(pagination.get('per_page')) == per_page)
                pages = _number(pagination.get('total_pages'))
                if pages is not None and total is not None:
                    metadata_valid &= pages == (total + per_page - 1) // per_page
            offset = (page - 1) * per_page
            invalid = self._observe_identity(data, rows if operation == 'contacts_search' else [], source, token)
            items, seen = [], set()
            for row in rows:
                try:
                    item = _project(row, operation, token)
                    require(item['id'] not in seen, 'invalid_item')
                    if operation == 'contacts_search' and 'contact_campaign_statuses' in row:
                        statuses, statuses_complete = _campaign_statuses(row['contact_campaign_statuses'], token)
                        item['contact_campaign_statuses'] = statuses
                        item['contact_campaign_statuses_complete'] = statuses_complete
                        invalid |= not statuses_complete
                    seen.add(item['id'])
                    items.append(item)
                except ExternalEmailError:
                    invalid = True
            items = items[:self.max_rows - offset]
            if total is not None and len(rows) != min(per_page, max(0, total - offset)):
                invalid = True
            source['has_more'] = total is not None and offset + len(rows) < total
            if source['has_more'] and page < 500 and offset + per_page < self.max_rows:
                source['next_page'] = page + 1
            complete = metadata_valid and not invalid and (page == 1 or total == 0) and len(items) == total
            reason = None
            if invalid:
                reason = 'partial_response'
            elif not metadata_valid:
                reason = 'pagination_unverified'
            elif not complete:
                reason = ('row_limit' if total > self.max_rows else
                          'more_pages' if source['has_more'] else 'page_slice')
            return self._envelope(source, page, items=items, total=total, reason=reason, complete=complete)
        except ExternalEmailError as error:
            return self._envelope(source, safe_page, reason=str(error))

    def people_search(self, filters, page=1, per_page=25):
        return self._search('people_search', filters, page, per_page)

    def contacts_search(self, query, page=1, per_page=25):
        return self._search('contacts_search', query, page, per_page)

    def sequences_search(self, query, page=1, per_page=25):
        return self._search('sequences_search', query, page, per_page)

    def profile(self):
        """Explicit free GET, no query/body. Never projects names, email or credits."""
        source = self._source('profile')
        try:
            data, token = self._call('GET', source['endpoint'], None, source)
            item = {'id': _strict_id(data['id'], token), 'team_id': _strict_id(data['team_id'], token)}
            return self._envelope(source, 1, items=[item], total=1, complete=True)
        except ExternalEmailError as error:
            return self._envelope(source, 1, reason=str(error))

    def credit_usage(self):
        source = self._source('credit_usage')
        try:
            data, token = self._call('POST', source['endpoint'], {}, source)
            stats = data.get('credit_usage_stats')
            require(isinstance(stats, dict) and 1 <= len(stats) <= 50, 'invalid_response')
            items = []
            invalid = self._observe_identity(data, [], source, token)
            for key, value in stats.items():
                if (key not in _CREDIT_TYPES or not isinstance(value, dict)
                        or not all(type(value.get(field)) is int and abs(value[field]) <= 10**12
                                   for field in ('limit', 'consumed', 'left_over'))):
                    invalid = True
                    continue
                items.append({'credit_type': key, **{field: value[field]
                              for field in ('limit', 'consumed', 'left_over')}})
            cycle = data.get('current_credit_cycle')
            if isinstance(cycle, dict) and set(cycle) >= {'start_date', 'end_date'} and all(
                    cycle[key] is None or _text(cycle[key], token, 64) is not None
                    for key in ('start_date', 'end_date')):
                source['current_credit_cycle'] = {key: cycle[key] for key in ('start_date', 'end_date')}
            else:
                invalid = True
            return self._envelope(source, 1, items=items, total=len(stats),
                complete=not invalid, reason='partial_response' if invalid else None)
        except ExternalEmailError as error:
            return self._envelope(source, 1, reason=str(error))
