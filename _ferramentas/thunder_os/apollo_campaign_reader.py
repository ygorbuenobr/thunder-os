"""S46 fixed-scope Apollo v1 observation reader; no provider or CRM writes.

Contacts sorting: https://docs.apollo.io/reference/search-for-contacts.
Messages are enumerated by the pinned MCP search tool with explicit totals,
then read back by exact IDs through REST. Pagination establishes bounded scope,
not an atomic source snapshot. Injected transports must preserve fixed-host TLS,
response-size/deadline limits and no redirects of apollo_readonly._https_request.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import http.client
import os
import ssl
import math
import re
import threading
import time
from urllib.parse import urlencode, parse_qs, urlsplit

from .apollo_readonly import _https_request, _integer
from .apollo_sequence_readonly import (_canonical, _headers, _rpc_response,
                                       _tool_payload, _ReadError)
from .contact_history import ContactHistoryReadonly
from .external_email_import import (APOLLO_SEQUENCE, APOLLO_TEAM, ExternalEmailError,
                                    email, identifier, require, timestamp)
from .instantly_reader import private_text
from .seqd_client import MAILBOX, SEQDClient
from email.utils import getaddresses

OWNER = '6a453e9f9ca1ee001407d14e'
SEQUENCE_PATH = '/api/v1/emailer_campaigns/search?q_name=Ygor&page=1&per_page=100'
CAMPAIGN = 'BR-APOLLO-01'
KIND = 'apollo_reader_snapshot'
KEY_FILE = '/root/.config/thunder/credentials/apollo_api_key.txt'
ENABLED_FILE = '/etc/thunder-os/apollo_reader_enabled'
INTERVAL = 60
COHORT_TTL = 3600
_AUTO = re.compile(r'^\s*(out of office|automatic reply|auto[ -]?reply|resposta autom[aá]tica|respuesta autom[aá]tica)\b', re.I)


_MCP_PROTOCOL = '2025-11-25'
_MCP_TOOL = 'apollo_emailer_messages_search'
_MCP_INITIALIZE = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': _MCP_PROTOCOL, 'capabilities': {},
    'clientInfo': {'name': 'thunder-os-campaign-readonly', 'version': '1.0'}}}
_MCP_NOTIFICATION = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}


def _validate_mcp(method, path, body):
    """Independent message-reader allowlist; the sequence reader is unchanged."""
    require(method == 'POST' and path == '/mcp' and isinstance(body, dict), 'apollo_mcp_endpoint_blocked')
    if body == _MCP_NOTIFICATION:
        return
    if body == _MCP_INITIALIZE:
        require(type(body.get('id')) is int, 'apollo_mcp_method_blocked')
        return
    require(set(body) == {'jsonrpc', 'id', 'method', 'params'} and body.get('jsonrpc') == '2.0'
            and body.get('method') == 'tools/call' and type(body.get('id')) is int,
            'apollo_mcp_method_blocked')
    params = body.get('params')
    require(isinstance(params, dict) and set(params) == {'name', 'arguments'}
            and params.get('name') == _MCP_TOOL, 'apollo_mcp_tool_blocked')
    args = params.get('arguments')
    require(isinstance(args, dict)
            and set(args) in ({'emailer_campaign_id', 'page', 'per_page'},
                              {'emailer_campaign_id', 'page', 'per_page', 'emailer_message_stats'})
            and ('emailer_message_stats' not in args or args['emailer_message_stats'] == ['sent'])
            and args.get('emailer_campaign_id') == APOLLO_SEQUENCE and args.get('per_page') == '100'
            and isinstance(args.get('page'), str) and len(args['page']) <= 3 and args['page'].isascii()
            and args['page'].isdigit() and str(int(args['page'])) == args['page']
            and 1 <= int(args['page']) <= 180
            and body['id'] == int(args['page']) + (181 if 'emailer_message_stats' in args else 1),
            'apollo_mcp_arguments_blocked')


def _mcp_https_request(method, path, body, headers, timeout):
    """Fixed MCP host, no redirects, bounded JSON/SSE; no old allowlist widening."""
    _validate_mcp(method, path, body)
    require(not any(os.environ.get(key) for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR')), 'tls_override_blocked')
    require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30, 'invalid_timeout')
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPSConnection('mcp.apollo.io', 443, timeout=timeout,
                                            context=ssl.create_default_context())
    try:
        connection.request(method, path, body=_canonical(body), headers=headers)
        remaining = deadline - time.monotonic()
        require(remaining > 0, 'apollo_read_budget_exhausted')
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        require(response.status != 429, 'apollo_rate_limited')
        require(response.status in (200, 202), 'apollo_mcp_http_error')
        response_headers = _headers(response.getheaders())
        content_type = response_headers.get('content-type', '')
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'apollo_read_budget_exhausted')
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, 1_000_001 - size))
            if not chunk:
                break
            size += len(chunk)
            require(size <= 1_000_000, 'response_too_large')
            chunks.append(chunk)
            if content_type.split(';')[0].strip().lower() == 'text/event-stream' and 'id' in body:
                accumulated = b''.join(chunks)
                ends = [accumulated.rfind(separator) + len(separator)
                        for separator in (b'\n\n', b'\r\n\r\n', b'\r\r') if separator in accumulated]
                if ends:
                    prefix = accumulated[:max(ends)]
                    if _rpc_response(prefix, content_type, body['id'], allow_pending=True) is not None:
                        return response.status, prefix, response_headers
        return response.status, b''.join(chunks), response_headers
    finally:
        connection.close()


def enabled():
    try:
        return private_text(ENABLED_FILE) == 'read-only-v1'
    except ExternalEmailError:
        return False


def _date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _line_endings(value):
    """Comparison only: preserve all words, spaces and original stored bodies."""
    return value.replace('\r\r\n', '\n').replace('\r\n', '\n').replace('\r', '\n')


def _timestamp_match(native, content):
    """Exact instants, or explicit loss of fractional seconds by content only."""
    timestamp(native)
    timestamp(content)
    native_instant, content_instant = _date(native), _date(content)
    if native_instant == content_instant:
        return 'exact'
    fraction = r'[Tt ]\d{2}:\d{2}:\d{2}[.,]\d+'
    if (re.search(fraction, native) and not re.search(fraction, content)
            and content_instant.microsecond == 0
            and native_instant.replace(microsecond=0) == content_instant):
        return 'content_second_truncation'
    return None


def _text(value):
    return value if isinstance(value, str) else ''


def _addresses(value):
    require(isinstance(value, list), 'invalid_recipients')
    return {email(row.get('email')) for row in value
            if isinstance(row, dict) and row.get('type') == 'to'}


class ApolloCampaignReadOnly:
    """Serialized collection with bounded requests, no retries, scoped cache only.

    request(method, path, body, headers, timeout) -> (status, bytes).
    mcp_request has the same signature and returns (status, bytes, headers).
    gmail is an optional SEQDClient-compatible port for flagged contacts only;
    its native calls are separately restricted to bounded GETs.
    """
    def __init__(self, credential_loader=None, *, request=None, gmail=None, mcp_request=None,
                 clock=None, monotonic=None, timeout=15, deadline=360,
                 max_calls=500, max_pages=180, cohort_ttl=COHORT_TTL):
        require(all(type(v) in (int, float) and math.isfinite(v) for v in
                    (timeout, deadline, cohort_ttl)) and 0 < timeout <= 30
                and 0 < deadline <= 360 and 3600 <= cohort_ttl <= 3600,
                'invalid_reader_bounds')
        require(type(max_calls) is int and 1 <= max_calls <= 1000
                and type(max_pages) is int and 1 <= max_pages <= 180, 'invalid_reader_bounds')
        self.loader = credential_loader or (lambda: private_text(KEY_FILE))
        self.request, self.gmail = request or _https_request, gmail
        self.mcp_request = mcp_request or _mcp_https_request
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.timeout, self.deadline = timeout, deadline
        self.max_calls, self.max_pages, self.cohort_ttl = max_calls, max_pages, cohort_ttl
        self.lock = threading.Lock()

    def _budget(self):
        remaining = self.ends - self.monotonic()
        require(not self.rate_limited, 'apollo_rate_limited')
        require(remaining > 0 and self.calls < self.max_calls, 'apollo_read_budget_exhausted')
        self.calls += 1
        return min(self.timeout, remaining)

    def _call(self, method, path, body=None):
        # Validate the entire request before credentials or transport access.
        valid = method == 'GET' and path in ('/api/v1/users/api_profile', SEQUENCE_PATH) and body is None
        if method == 'POST' and path == '/api/v1/contacts/search' and isinstance(body, dict):
            valid = (set(body) == {'page', 'per_page', 'sort_by_field', 'sort_ascending'}
                     and type(body['page']) is int and 1 <= body['page'] <= self.max_pages
                     and body['per_page'] == 100 and body['sort_by_field'] == 'contact_created_at'
                     and body['sort_ascending'] is True)
        if method == 'GET' and body is None:
            if path.startswith('/api/v1/emailer_messages/search?'):
                query = parse_qs(urlsplit(path).query)
                ids = query.get('ids[]')
                valid = (isinstance(ids, list) and 1 <= len(ids) <= 10 and len(set(ids)) == len(ids)
                         and all(identifier(v) and v in getattr(self, 'observed_ids', set()) for v in ids)
                         and path == self._native_message_path(ids))
        if method == 'POST' and path == '/api/v1/emailer_messages/get_content' and isinstance(body, dict):
            ids = body.get('ids')
            valid = (set(body) == {'ids', 'body_format'} and body.get('body_format') == 'plain'
                     and isinstance(ids, list) and 1 <= len(ids) <= 10
                     and all(identifier(v) for v in ids) and len(set(ids)) == len(ids))
        require(valid, 'apollo_endpoint_blocked')
        remaining = self._budget()
        try:
            token = self.loader()
            require(isinstance(token, str) and 0 < len(token) <= 4096
                    and not any(c.isspace() for c in token), 'credential_unavailable')
            status, raw = self.request(method, path, body,
                {'x-api-key': token, 'Content-Type': 'application/json'}, remaining)
            if status == 429:
                self.rate_limited = True
            require(status != 429, 'apollo_rate_limited')
            require(type(status) is int and status == 200, 'apollo_http_error')
            require(isinstance(raw, bytes) and len(raw) <= 1_000_000, 'invalid_response')
            data = json.loads(raw)
            require(isinstance(data, dict), 'invalid_response')
            require(self.monotonic() < self.ends, 'apollo_read_budget_exhausted')
            return data
        except ExternalEmailError:
            raise
        except Exception:
            raise ExternalEmailError('apollo_read_unavailable') from None

    @staticmethod
    def _native_message_path(ids):
        return '/api/v1/emailer_messages/search?' + urlencode(
            [('ids[]', ident) for ident in ids] + [('page', 1), ('per_page', 10)])

    def _mcp_call(self, message, expected_id):
        _validate_mcp('POST', '/mcp', message)
        remaining = self._budget()
        try:
            token = self.loader()
            require(isinstance(token, str) and 0 < len(token) <= 4096 and token.isascii()
                    and all(33 <= ord(c) <= 126 for c in token), 'credential_unavailable')
            headers = {'X-Api-Key': token, 'Content-Type': 'application/json',
                       'Accept': 'application/json, text/event-stream'}
            if expected_id != 1:
                headers['MCP-Protocol-Version'] = _MCP_PROTOCOL
            if self.mcp_session is not None:
                headers['Mcp-Session-Id'] = self.mcp_session
            status, raw, response_headers = self.mcp_request('POST', '/mcp', message, headers, remaining)
            require(self.monotonic() < self.ends, 'apollo_read_budget_exhausted')
            if status == 429:
                self.rate_limited = True
            require(status != 429, 'apollo_rate_limited')
            require(type(status) is int and status == (202 if expected_id is None else 200), 'apollo_mcp_http_error')
            response_headers = _headers(response_headers)
            protocol = response_headers.get('mcp-protocol-version')
            require(protocol is None or protocol == _MCP_PROTOCOL, 'apollo_mcp_protocol_unsupported')
            if 'mcp-session-id' in response_headers:
                session = response_headers['mcp-session-id']
                require(0 < len(session) <= 256 and session.isascii()
                        and all(33 <= ord(c) <= 126 for c in session)
                        and (self.mcp_session is None or session == self.mcp_session), 'apollo_mcp_invalid_session')
                self.mcp_session = session
            if expected_id is None:
                require(isinstance(raw, bytes) and raw == b'', 'apollo_mcp_invalid_notification_ack')
                return None
            return _rpc_response(raw, response_headers.get('content-type', ''), expected_id)
        except ExternalEmailError as exc:
            if exc.code == 'apollo_rate_limited':
                self.rate_limited = True
            raise
        except Exception:
            raise ExternalEmailError('apollo_mcp_invalid_response') from None

    def _mcp_start(self):
        initialized = self._mcp_call(deepcopy(_MCP_INITIALIZE), 1)
        require(initialized.get('protocolVersion') == _MCP_PROTOCOL
                and isinstance(initialized.get('capabilities'), dict)
                and isinstance(initialized['capabilities'].get('tools'), dict), 'apollo_mcp_protocol_unsupported')
        self._mcp_call(deepcopy(_MCP_NOTIFICATION), None)

    def _mcp_page(self, page, *, sent_only=False):
        number = page + (181 if sent_only else 1)
        message = {'jsonrpc': '2.0', 'id': number, 'method': 'tools/call', 'params': {
            'name': _MCP_TOOL, 'arguments': {'emailer_campaign_id': APOLLO_SEQUENCE,
                                          'page': str(page), 'per_page': '100'}}}
        if sent_only:
            message['params']['arguments']['emailer_message_stats'] = ['sent']
        try:
            return _tool_payload(self._mcp_call(message, number))
        except _ReadError:
            raise ExternalEmailError('apollo_mcp_invalid_response') from None

    @staticmethod
    def _scope(row, *, message=False):
        require(isinstance(row, dict), 'invalid_record')
        require('team_id' not in row or row['team_id'] == APOLLO_TEAM, 'apollo_team_mismatch')
        if message:
            require(row.get('emailer_campaign_id') == APOLLO_SEQUENCE, 'message_sequence_mismatch')
            require(row.get('user_id') == OWNER, 'apollo_owner_mismatch')

    def _contacts(self, result, previous):
        now = self.clock()
        cached = (previous or {}).get('cohort_cache')
        if isinstance(cached, dict):
            try:
                fresh = 0 <= (now - _date(cached['observed_at'])).total_seconds() < self.cohort_ttl
                require(cached.get('account_id') == APOLLO_TEAM
                        and cached.get('owner_id') == OWNER
                        and cached.get('external_campaign_id') == APOLLO_SEQUENCE
                        and cached.get('complete') is True and fresh, 'invalid_cohort_cache')
                members = self._members(cached['members'])
                result['leads'] = members
                result['cohort_cache'] = deepcopy(cached)
                result['coverage'].update(contacts_complete=True, contacts_cached=True,
                    contacts_observed_at=cached['observed_at'], contacts_source_total=cached['source_total'])
                return
            except (ExternalEmailError, ValueError, TypeError, KeyError):
                pass
        seen, members, total = set(), [], None
        for page in range(1, self.max_pages + 1):
            data = self._call('POST', '/api/v1/contacts/search', {
                'page': page, 'per_page': 100, 'sort_by_field': 'contact_created_at', 'sort_ascending': True})
            rows, pagination = data.get('contacts'), data.get('pagination')
            require(isinstance(rows, list) and len(rows) <= 100 and isinstance(pagination, dict), 'invalid_contact_page')
            count = pagination.get('total_entries')
            require(type(count) is int and count >= 0 and pagination.get('page') == page
                    and pagination.get('per_page') == 100, 'invalid_contact_pagination')
            require(total is None or total == count, 'apollo_contacts_source_changed')
            total = count
            result['coverage']['contacts_pages'] = page
            for row in rows:
                self._scope(row)
                ident = row.get('id')
                require(identifier(ident) and ident not in seen, 'duplicate_or_invalid_contact')
                seen.add(ident)
                statuses = row.get('contact_campaign_statuses')
                require(isinstance(statuses, list) and all(isinstance(s, dict) for s in statuses),
                        'contact_membership_unknown')
                matches = [s for s in statuses if s.get('emailer_campaign_id') == APOLLO_SEQUENCE]
                require(len(matches) <= 1, 'duplicate_membership')
                if matches:
                    members.append(self._lead(row, matches[0]))
            result['leads'] = deepcopy(members)
            if len(seen) == total:
                result['coverage'].update(contacts_complete=True, contacts_cached=False,
                    contacts_observed_at=self.clock().isoformat(), contacts_source_total=total)
                result['cohort_cache'] = {'account_id': APOLLO_TEAM, 'owner_id': OWNER,
                    'external_campaign_id': APOLLO_SEQUENCE, 'complete': True, 'source_total': total,
                    'observed_at': result['coverage']['contacts_observed_at'], 'members': deepcopy(members)}
                return
            require(len(rows) == 100 and len(seen) < total, 'incomplete_contacts')
        raise ExternalEmailError('apollo_contacts_page_limit')

    @staticmethod
    def _lead(row, membership):
        primary = row.get('email')
        primary = email(primary) if primary else None
        return {'id': row['id'], 'name': row.get('name') or ' '.join(filter(None,
                    [_text(row.get('first_name')), _text(row.get('last_name'))])),
                'first_name': row.get('first_name'), 'last_name': row.get('last_name'),
                'title': row.get('title'),
                'company': (row.get('organization') or row.get('account') or {}).get('name'),
                'email': primary, 'original_email': row.get('email'), 'contact_emails': deepcopy(row.get('contact_emails', [])), 'linkedin_url': row.get('linkedin_url'),
                'membership': deepcopy(membership), 'membership_status': membership.get('status'),
                'events': []}

    def _members(self, rows):
        require(isinstance(rows, list), 'invalid_cohort_cache')
        members, seen = [], set()
        for row in rows:
            require(isinstance(row, dict) and identifier(row.get('id')) and row['id'] not in seen
                    and isinstance(row.get('membership'), dict)
                    and row['membership'].get('emailer_campaign_id') == APOLLO_SEQUENCE,
                    'invalid_cohort_cache')
            seen.add(row['id'])
            item = deepcopy(row)
            item['events'] = []
            members.append(item)
        return members

    def _messages(self, result):
        leads, threads = {row['id']: row for row in result['leads']}, {}
        coverage = result['coverage']
        coverage['messages_transport'] = 'mcp_enumeration_native_id_readback'
        try:
            self._mcp_start()
        except Exception as exc:
            coverage['reasons'].append(exc.code if isinstance(exc, ExternalEmailError)
                                       else 'apollo_mcp_read_unavailable')
            return
        # The sent partition has independently observed stable pagination. Its
        # originals/replies survive an incomplete unfiltered queue enumeration.
        self._enumerate_messages(result, leads, threads, sent_only=True)
        read_threads = set(threads)
        for ident, thread in read_threads:
            self._reply(result, leads[ident], thread, threads[(ident, thread)])
        self._enumerate_messages(result, leads, threads, sent_only=False)
        for (ident, thread), originals in threads.items():
            if (ident, thread) not in read_threads:
                self._reply(result, leads[ident], thread, originals)
        coverage['native_message_ids_observed'] = len(self.hydrated_messages)

    def _enumerate_messages(self, result, leads, threads, *, sent_only):
        coverage, total, seen_query = result['coverage'], None, set()
        prefix = 'sent_messages' if sent_only else 'messages'
        metadata_key = 'sent_message_metadata_observed' if sent_only else 'message_metadata_observed'
        try:
            for page in range(1, self.max_pages + 1):
                data = self._mcp_page(page, sent_only=sent_only)
                items, pagination = data.get('emailer_messages'), data.get('pagination')
                require(isinstance(items, list) and len(items) <= 100
                        and isinstance(pagination, dict), 'invalid_message_page')
                count, pages = _integer(pagination.get('total_entries')), _integer(pagination.get('total_pages'))
                require(_integer(pagination.get('page')) == page and _integer(pagination.get('per_page')) == 100
                        and 0 <= count <= 18000 and pages == (count + 99) // 100,
                        'invalid_message_pagination')
                require(total is None or total == count, 'apollo_messages_source_changed')
                total = count
                coverage['sent_messages_total' if sent_only else 'messages_total_entries'] = total
                require(len(items) == min(100, max(0, count - (page - 1) * 100)), 'incomplete_message_page')
                page_ids = set()
                for row in items:
                    self._scope(row)
                    require(row.get('emailer_campaign_id') == APOLLO_SEQUENCE, 'message_sequence_mismatch')
                    require('user_id' not in row or row['user_id'] == OWNER, 'apollo_owner_mismatch')
                    require(identifier(row.get('id')) and row['id'] not in seen_query
                            and row['id'] not in page_ids, 'duplicate_or_invalid_message')
                    require(row.get('contact_id') in leads, 'message_contact_outside_cohort')
                    require(row['id'] not in self.message_contacts
                            or self.message_contacts[row['id']] == row['contact_id'],
                            'message_contact_changed_between_queries')
                    page_ids.add(row['id'])
                seen_query.update(page_ids)
                self.observed_ids.update(page_ids)
                self.message_contacts.update({row['id']: row['contact_id'] for row in items})
                coverage.update({prefix + '_pages': page, metadata_key: len(seen_query)})
                # Each native ID is attempted at most once per collection, even
                # after a read failure; all-query overlap is not a second retry.
                pending = [row for row in items if row['id'] not in self.native_attempted_ids]
                for start in range(0, len(pending), 10):
                    batch = {row['id']: row for row in pending[start:start + 10]}
                    self.native_attempted_ids.update(batch)
                    self._hydrate(result, batch, leads, threads)
                if page >= pages:
                    require(len(seen_query) == total, 'incomplete_message_page')
                    require(seen_query <= self.hydrated_messages.keys(), 'native_messages_incomplete')
                    if sent_only:
                        require(all(self.hydrated_messages[ident] == 'email_sent' for ident in seen_query),
                                'sent_filtered_content_unverified')
                    coverage[prefix + '_complete'] = True
                    break
            else:
                raise ExternalEmailError('apollo_messages_page_limit')
        except Exception as exc:
            coverage['reasons'].append(exc.code if isinstance(exc, ExternalEmailError)
                                       else 'apollo_mcp_read_unavailable')

    def _hydrate(self, result, batch, leads, threads):
        data = self._call('GET', self._native_message_path(list(batch)))
        items = data.get('emailer_messages')
        require(isinstance(items, list) and len(items) == len(batch), 'native_message_ids_mismatch')
        native, seen = [], set()
        for row in items:
            self._scope(row, message=True)
            require(identifier(row.get('id')) and row['id'] in batch and row['id'] not in seen,
                    'native_message_ids_mismatch')
            require(row.get('contact_id') == batch[row['id']]['contact_id'], 'native_message_contact_mismatch')
            seen.add(row['id'])
            native.append(row)
        require(seen == set(batch), 'native_message_ids_mismatch')
        completed = [row['id'] for row in native if row.get('status') == 'completed']
        contents = {}
        if completed:
            data = self._call('POST', '/api/v1/emailer_messages/get_content', {'ids': completed, 'body_format': 'plain'})
            items = data.get('emailer_messages')
            require(isinstance(items, list) and len(items) <= len(completed), 'invalid_content_response')
            for row in items:
                require(isinstance(row, dict) and row.get('id') in completed and row['id'] not in contents,
                        'invalid_content_response')
                contents[row['id']] = row
        for row in native:
            lead = leads[row['contact_id']]
            try:
                event = self._event(row, contents.get(row['id']), lead)
            except ExternalEmailError as exc:
                result['coverage']['reasons'].append(exc.code)
                event = self._event(row, None, lead)
            lead['events'].append(event)
            self.hydrated_messages[row['id']] = event['event_type']
            if row.get('status') == 'completed' and event['event_type'] != 'email_sent':
                result['coverage']['reasons'].append('completed_content_unverified')
            if row.get('replied') is True:
                signal = {**deepcopy(event), 'provider_id': row['id'] + ':reply_signal',
                    'event_type': 'reply_signal', 'text': '', 'content_complete': False,
                    'human_reply': None, 'occurred_at': None, 'receipt': False,
                    'provider_reply_class': row.get('reply_class')}
                lead['events'].append(signal)
                thread = row.get('provider_thread_id')
                if event['event_type'] == 'email_sent' and email(event['sender']) != MAILBOX:
                    result['coverage']['reasons'].append('gmail_reply_mailbox_unavailable')
                elif event['event_type'] == 'email_sent' and isinstance(thread, str) and re.fullmatch(r'[a-f0-9]{1,64}', thread):
                    threads.setdefault((lead['id'], thread), []).append(event)
                else:
                    result['coverage']['reasons'].append('reply_original_unavailable')

    @staticmethod
    def _event(meta, content, lead):
        event = {'provider_id': meta['id'], 'provider': 'apollo', 'gmail_message_id': None,
                 'account_id': APOLLO_TEAM, 'external_campaign_id': APOLLO_SEQUENCE, 'lead_email': lead['email'],
                 'reply_class': meta.get('reply_class'), 'bounce': meta.get('bounce'),
                 'direction': 'outgoing', 'ref_sent': meta['id'], 'sender': meta.get('from_email'), 'source_association': 'canonical_contact', 'event_type': 'scheduled' if meta.get('status') == 'scheduled'
                    else 'provider_status', 'provider_status': meta.get('status'),
                 'occurred_at': meta.get('due_at'), 'text': '', 'subject': _text(meta.get('subject')),
                 'content_complete': False, 'provider_message_id': meta.get('provider_message_id'),
                 'provider_thread_id': meta.get('provider_thread_id'), 'human_reply': None,
                 'receipt': False, 'source': {'provider': 'apollo', 'account_id': APOLLO_TEAM,
                    'external_campaign_id': APOLLO_SEQUENCE, 'campaign_id': CAMPAIGN, 'provider_id': meta['id']},
                 'source_url': 'https://app.apollo.io/#/contacts/' + lead['id']}
        if meta.get('status') != 'completed' or content is None:
            return event
        require(content.get('status') == 'completed' and content.get('body_format') == 'plain',
                'not_completed_plain_email')
        try:
            email(meta.get('from_email'))
        except ExternalEmailError:
            raise ExternalEmailError('invalid_sender') from None
        require(lead['email'] in _addresses(content.get('recipients'))
                and email(meta.get('to_email')) == lead['email'], 'recipient_identity_mismatch')
        body = content.get('body')
        require(isinstance(body, str) and bool(body.strip()) and len(body) <= 200_000, 'invalid_email_content')
        match = _timestamp_match(meta.get('completed_at'), content.get('sent_at'))
        require(match is not None, 'sent_timestamp_mismatch')
        event.update(event_type='email_sent', occurred_at=meta['completed_at'], text=body,
                     native_completed_at=meta['completed_at'], content_sent_at=content['sent_at'],
                     timestamp_match=match,
                     subject=_text(content.get('subject')), content_complete=True, receipt=True)
        return event

    def _reply(self, result, lead, thread_id, originals, *, discovered=False):
        if self.gmail is None:
            result['coverage']['reasons'].append('gmail_reply_reader_unavailable')
            return
        try:
            require(all(email(original.get('sender')) == MAILBOX for original in originals),
                    'gmail_reply_mailbox_unavailable')
            self._budget()
            require(not self.gmail_rate_limited, 'gmail_rate_limited')
            response = self._gmail.conversation(thread_id)
            require(self.monotonic() < self.ends, 'apollo_read_budget_exhausted')
            require(isinstance(response, dict) and response.get('mailbox') == MAILBOX
                    and response.get('status') == 'ok', 'gmail_identity_mismatch')
            thread = response.get('thread')
            require(isinstance(thread, dict) and thread.get('id') == thread_id
                    and thread.get('context_only') is True and thread.get('receipt_eligible') is False
                    and isinstance(thread.get('messages'), list) and len(thread['messages']) <= 100,
                    'gmail_thread_mismatch')
            require(all(isinstance(m, dict) and isinstance(m.get('id'), str) for m in thread['messages'])
                    and len({m['id'] for m in thread['messages']}) == len(thread['messages']), 'gmail_duplicate_message')
            pairs = []
            for m in thread['messages']:
                event = ContactHistoryReadonly._gmail_event(m, thread_id, {lead['email']})
                if event is None and isinstance(m, dict):
                    actual = {addr.lower() for _, addr in getaddresses([_text(m.get('from'))])}
                    if len(actual) == 1 and MAILBOX not in actual:
                        event = ContactHistoryReadonly._gmail_event(m, thread_id, actual)
                pairs.append((m, event))
            matched, identity_only = [], []
            for original in originals:
                for raw, event in pairs:
                    if (event and event['direction'] == 'outgoing' and event['body_complete']
                            and raw.get('id') == original['provider_message_id']
                            and event['subject'] == original['subject']
                            and event['date']):
                        proof = {**event, 'apollo_provider_id': original['provider_id'],
                                 'apollo_occurred_at': original['occurred_at']}
                        if _line_endings(event['text']) == _line_endings(original['text']):
                            proof['body_match'] = 'literal' if event['text'] == original['text'] else 'line_endings_only'
                            matched.append(proof)
                        else:
                            proof['body_match'] = 'different'
                            identity_only.append(proof)
            context_only = not matched and bool(identity_only)
            anchors = matched or identity_only
            if not matched and not discovered and not context_only:
                raise ExternalEmailError('gmail_outbound_original_mismatch')
            if context_only:
                result['coverage']['reasons'].append('gmail_outbound_original_mismatch')
            elif not matched:
                result['coverage']['reasons'].append('gmail_discovered_thread_context_only')
            if (thread.get('complete') is not True or thread.get('truncated') is not False
                    or thread.get('coverage', {}).get('reasons') != []):
                result['coverage']['reasons'].append('gmail_thread_partial')
            found = False
            for raw, event in pairs:
                if not event or event['direction'] != 'incoming':
                    continue
                require(event['date'] and (discovered or any(_date(event['date']) >= _date(sent['date']) for sent in anchors
                        if sent['date'])), 'gmail_reply_date_unverified')
                headers = raw.get('headers')
                auto = bool(_AUTO.search(event['subject']))
                if isinstance(headers, list):
                    headers = {h.get('name', ''): h.get('value', '') for h in headers if isinstance(h, dict)}
                if isinstance(headers, dict):
                    values = {str(k).lower(): str(v).lower() for k, v in headers.items()}
                    auto = auto or values.get('auto-submitted', 'no') != 'no' or 'auto-replied' in values.get('precedence', '')
                sender = next(iter(addr.lower() for _, addr in getaddresses([_text(raw.get('from'))])))
                third_party = sender != lead['email']
                kind = 'third_party_reply' if third_party else ('auto_reply_received' if auto else 'unclassified_reply')
                lead['events'].append({'provider_id': raw['id'], 'provider': 'gmail', 'gmail_message_id': raw['id'],
                    'account_id': MAILBOX, 'external_campaign_id': APOLLO_SEQUENCE,
                    'lead_email': lead['email'] if not third_party and matched else None,
                    'ref_sent': matched[0]['apollo_provider_id'] if matched else None,
                    'gmail_original_date': anchors[0]['date'] if anchors else None,
                    'gmail_original_body_match': anchors[0]['body_match'] if anchors else None,
                    'apollo_completed_at': anchors[0]['apollo_occurred_at'] if anchors else None,
                    'direction': 'incoming', 'automatic': auto, 'sender': sender, 'source_association': 'thread_context' if third_party or not matched else 'canonical_contact',
                    'event_type': kind,
                    'occurred_at': event['date'], 'text': event['text'], 'subject': event['subject'],
                    'content_complete': event['body_complete'], 'human_reply': False if auto and not third_party and matched else None,
                    'classification_source': 'automatic_marker' if auto else 'unknown',
                    'provider_message_id': raw['id'], 'provider_thread_id': thread_id,
                    'receipt': event['body_complete'] and bool(matched) and not third_party, 'source_url': event['source_url'],
                    'source': {'provider': 'gmail', 'account_id': MAILBOX,
                        'external_campaign_id': APOLLO_SEQUENCE, 'campaign_id': CAMPAIGN,
                        'provider_id': raw['id']}})
                found = True
                if not event['body_complete']:
                    result['coverage']['reasons'].append('gmail_reply_body_partial')
            if not found:
                result['coverage']['reasons'].append('reply_original_unavailable')
                if not discovered:
                    self._discover(result, lead, thread_id, originals)
        except Exception as exc:
            result['coverage']['reasons'].append('gmail_rate_limited' if self.gmail_rate_limited else
                (exc.code if isinstance(exc, ExternalEmailError) else 'gmail_reply_read_unavailable'))

    def _discover(self, result, lead, known_thread, originals):
        """Flagged contacts only; discovery provides context, never attribution."""
        if not callable(getattr(self._gmail, 'contact_search', None)):
            result['coverage']['reasons'].append('gmail_reply_discovery_unavailable')
            return
        cursor, cursors, threads = None, set(), {known_thread}
        for page in range(5):
            self._budget()
            require(not self.gmail_rate_limited, 'gmail_rate_limited')
            data = self._gmail.contact_search(lead['email'], cursor=cursor)
            require(self.monotonic() < self.ends, 'apollo_read_budget_exhausted')
            require(data.get('mailbox') == MAILBOX and data.get('status') == 'ok'
                    and data.get('email') == lead['email'] and data.get('context_only') is True
                    and data.get('receipt_eligible') is False
                    and isinstance(data.get('messages'), list) and len(data['messages']) <= 10,
                    'gmail_discovery_invalid')
            for row in data['messages']:
                thread = row.get('thread_id')
                require(isinstance(thread, str) and re.fullmatch(r'[a-f0-9]{1,64}', thread), 'gmail_thread_mismatch')
                if thread not in threads:
                    require(len(threads) < 20, 'gmail_discovery_thread_limit')
                    threads.add(thread)
                    self._reply(result, lead, thread, originals, discovered=True)
            next_cursor = data.get('next_cursor')
            if next_cursor is None:
                return
            require(isinstance(next_cursor, str) and next_cursor not in cursors, 'gmail_discovery_cursor_repeated')
            cursors.add(next_cursor)
            cursor = next_cursor
        result['coverage']['reasons'].append('gmail_discovery_page_limit')

    def collect(self, previous=None):
        require(self.lock.acquire(blocking=False), 'apollo_reader_busy')
        try:
            self.calls, self.ends = 0, self.monotonic() + self.deadline
            self.rate_limited, self.mcp_session, self.observed_ids = False, None, set()
            self.native_attempted_ids, self.hydrated_messages, self.message_contacts = set(), {}, {}
            self.gmail_rate_limited = False
            self._gmail = self.gmail
            if isinstance(self.gmail, SEQDClient):
                original = self.gmail
                def bounded(method, path, body, headers, timeout):
                    remaining = self.ends - self.monotonic()
                    require(not self.gmail_rate_limited and remaining > 0 and method == 'GET'
                            and (re.fullmatch(r'/v1/threads/[a-f0-9]{1,64}', path)
                                 or path.startswith('/v1/contact-search?')), 'gmail_read_blocked')
                    response = original._request(method, path, body, headers, min(4, remaining, timeout))
                    if response[0] == 429:
                        self.gmail_rate_limited = True
                        raise ExternalEmailError('gmail_rate_limited')
                    return response
                self._gmail = SEQDClient(original._loader, request=bounded)
            result = {'campaign_id': CAMPAIGN, 'account_id': APOLLO_TEAM, 'owner_id': OWNER,
                'external_campaign_id': APOLLO_SEQUENCE, 'observed_at': self.clock().isoformat(),
                'coverage': {'complete': False, 'contacts_complete': False, 'messages_complete': False,
                    'sent_messages_complete': False, 'sent_messages_total': None,
                    'contacts_pages': 0, 'messages_pages': 0, 'contents_complete': False, 'reasons': [],
                    'scope': 'literal_sequence_members_and_scoped_messages', 'atomic_snapshot': False},
                'leads': [], 'metrics': None}
            try:
                profile = self._call('GET', '/api/v1/users/api_profile')
                require(profile.get('team_id') == APOLLO_TEAM, 'apollo_team_mismatch')
                require(identifier(profile.get('id')), 'apollo_profile_id_invalid')
                result['coverage']['authenticated_user_id'] = profile['id']
                # A team API key's actor need not own the campaign. Prove that
                # separate ownership on every collection, including cached ones.
                sequences = self._call('GET', SEQUENCE_PATH).get('emailer_campaigns')
                require(isinstance(sequences, list) and len(sequences) <= 100
                        and all(isinstance(row, dict) for row in sequences), 'invalid_sequence_response')
                matches = [row for row in sequences if row.get('id') == APOLLO_SEQUENCE]
                require(len(matches) == 1, 'apollo_sequence_missing_or_duplicate')
                sequence = matches[0]
                require(sequence.get('user_id') == OWNER, 'apollo_owner_mismatch')
                require(sequence.get('archived') is False, 'apollo_sequence_archived_or_unknown')
                self._scope(sequence)
                result['coverage']['sequence_owner_verified'] = True
                overall_end = self.ends
                self.ends = min(overall_end, self.monotonic() + 240)
                self._contacts(result, previous)
                self.ends = overall_end
                self._messages(result)
                result['coverage']['contents_complete'] = 'completed_content_unverified' not in result['coverage']['reasons']
                result['observed_at'] = self.clock().isoformat()
                result['coverage']['messages_observed_at'] = result['observed_at']
            except Exception as exc:
                result['coverage']['reasons'].append(exc.code if isinstance(exc, ExternalEmailError)
                                                      else 'apollo_read_unavailable')
            cov = result['coverage']
            cov['reasons'] = sorted(set(cov['reasons']))
            if not cov['messages_complete']:
                cov['contents_complete'] = False
            cov['complete'] = cov['contacts_complete'] and cov['messages_complete'] and not cov['reasons']
            cov['provider_requests'] = self.calls
            for lead in result['leads']:
                lead['events'].sort(key=lambda e: (e.get('occurred_at') or '', e['provider_id']))
            counts = Counter(e['event_type'] for lead in result['leads'] for e in lead['events'])
            result['metrics'] = {'observed_leads': len(result['leads']), 'sent_messages': counts['email_sent'],
                'human_replies': counts['reply_received'], 'automatic_replies': counts['auto_reply_received'],
                'unclassified_replies': counts['unclassified_reply'], 'reply_signals': counts['reply_signal'], 'third_party_replies': counts['third_party_reply'],
                'complete': cov['complete'],
                'membership_statuses': dict(Counter(lead.get('membership_status') or 'unknown' for lead in result['leads']))}
            return result
        finally:
            self.lock.release()


class ApolloCampaignReader:
    """Repository cache only on snapshot(); explicit private enable check per use."""
    def __init__(self, repo, provider=None, *, enable_check=enabled, clock=None):
        self.repo, self.provider = repo, provider or ApolloCampaignReadOnly(gmail=SEQDClient())
        self.enable_check = enable_check
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock, self.stop_event = threading.Lock(), threading.Event()
        self.thread = None

    def refresh(self):
        if not self.enable_check() or not self.lock.acquire(blocking=False):
            return
        try:
            previous = self.repo.get_state(KIND, CAMPAIGN) or {}
            now = self.clock()
            if previous.get('last_attempt_at') and 0 <= (now - _date(previous['last_attempt_at'])).total_seconds() < INTERVAL:
                return
            try:
                data = self.provider.collect(previous=deepcopy(previous))
                require(self.enable_check(), 'apollo_reader_disabled')
                state = {**data, 'last_attempt_at': now.isoformat(),
                    'last_success_at': self.clock().isoformat(), 'error_code': None,
                    'status': 'ok' if data['coverage']['complete'] else 'partial'}
                if not data['coverage']['complete']:
                    state['error_code'] = next(iter(data['coverage'].get('reasons', [])), 'incomplete_source')
                    # Failure never renews a successful observation or cached cohort.
                    state['last_success_at'] = previous.get('last_success_at')
                    if 'cohort_cache' not in state and previous.get('cohort_cache'):
                        state['cohort_cache'] = deepcopy(previous['cohort_cache'])
                    fresh_contacts = (data['coverage'].get('contacts_complete') is True
                                      and data['coverage'].get('contacts_cached') is False)
                    has_events = any(lead.get('events') for lead in data.get('leads', []))
                    if previous.get('leads') and not fresh_contacts and not has_events:
                        # collect() reports failures as partial data, not exceptions.
                        # Cache-only members are not a new observation of events.
                        for key in ('leads', 'metrics', 'observed_at', 'last_success_at'):
                            state[key] = deepcopy(previous.get(key))
                        state['status'] = 'error'
                        state['coverage']['retained_observation'] = True
                        state['coverage']['retained_observed_at'] = previous.get('observed_at')
                        if isinstance(state.get('metrics'), dict):
                            state['metrics']['complete'] = False

            except Exception as exc:
                state = deepcopy(previous)
                state.update(last_attempt_at=now.isoformat(), status='error',
                    error_code=exc.code if isinstance(exc, ExternalEmailError) else 'apollo_read_unavailable')
            if self.enable_check():
                self.repo.put_state(KIND, CAMPAIGN, state)
        finally:
            self.lock.release()

    def snapshot(self):
        active = self.enable_check()
        state = self.repo.get_state(KIND, CAMPAIGN) if active else None
        result = {'schema_version': 1, 'read_only': True, 'enabled': active,
            'status': 'pending' if active else 'disabled', 'last_attempt_at': None,
            'last_success_at': None, 'observed_at': None, 'poll_interval_seconds': INTERVAL,
            'campaign_id': CAMPAIGN, 'account_id': APOLLO_TEAM, 'owner_id': OWNER,
            'external_campaign_id': APOLLO_SEQUENCE,
            'coverage': {'complete': False, 'contacts_complete': False, 'messages_complete': False,
                         'sent_messages_complete': False, 'sent_messages_total': None},
            'metrics': None, 'leads': [], 'error_code': None, 'stale': False,
            'crm_write_enabled': False, 'external_actions_enabled': False}
        if state:
            for key in result.keys() & state.keys() - {'enabled', 'campaign_id', 'account_id', 'owner_id',
                    'external_campaign_id', 'read_only', 'crm_write_enabled', 'external_actions_enabled'}:
                result[key] = deepcopy(state[key])
            observed = result.get('observed_at')
            try:
                result['stale'] = not observed or not 0 <= (self.clock() - _date(observed)).total_seconds() <= 180
            except (ValueError, TypeError):
                result['stale'] = True
            if result['status'] in ('partial', 'error'):
                result['coverage']['complete'] = False
                if isinstance(result['metrics'], dict):
                    result['metrics']['complete'] = False
            if result['stale']:
                if isinstance(result['metrics'], dict):
                    result['metrics']['complete'] = False
                result['status'] = 'stale'
                result['coverage']['complete'] = False
        return result

    def start(self):
        if self.thread is not None:
            return
        def run():
            while not self.stop_event.is_set():
                try:
                    self.refresh()
                except Exception:
                    pass
                self.stop_event.wait(INTERVAL)
        self.thread = threading.Thread(target=run, name='apollo-read-only', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
