"""Apollo v1 sent outreach reader. Two fixed, zero-credit read endpoints only."""
from collections.abc import Mapping
import http.client
import json
import math
import os
import ssl
from urllib.parse import urlencode

from .external_email_import import (APOLLO_TEAM, APOLLO_SEQUENCE, ExternalEmailError,
    contact_emails, email, identifier, plan_contact, require, timestamp)


def contract_status():
    return {'provider': 'apollo', 'campaign_id': 'BR-APOLLO-01',
            'external_campaign_id': APOLLO_SEQUENCE, 'account_id': APOLLO_TEAM,
            'api_version': 'v1', 'enabled': False, 'writer_enabled': False,
            'status': 'configuration_pending', 'configuration_pending': True,
            'provider_verified': False, 'email_logged': False,
            'email_dedupe': 'unknown', 'canary': 'retained_permission_modification',
            'required_scopes': ['emailer_messages_search', 'emailer_messages_get_content']}


def dry_run(payload=None):
    """Structural inspection, including private caller inputs; never source proof."""
    sample = {'id': 'synthetic-apollo-contact', 'email': 'contact@example.invalid',
              'contact_emails': [], 'first_name': 'Synthetic'}
    contact = sample if payload is None else payload
    try:
        primary, emails = contact_emails(contact)
        result = {'structurally_valid': True, 'has_primary_email': primary is not None,
                  'email_identifiers': len(emails)}
    except (ExternalEmailError, TypeError, KeyError):
        result = {'structurally_valid': False, 'error_code': 'invalid_contact'}
    return {**result, 'status': 'structural_dry_run', 'simulation': True,
            'needs_provider_readback': True, 'provider_verified': False,
            'email_logged': False, 'writes': 0, 'db_writes': 0, 'persisted': False}


def _https_request(method, path, body, headers, timeout):
    require(not any(os.environ.get(key) for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR')),
            'tls_override_blocked')
    connection = http.client.HTTPSConnection('api.apollo.io', 443, timeout=timeout,
                                            context=ssl.create_default_context())
    try:
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        require(len(raw) <= 1_000_000, 'response_too_large')
        return response.status, raw
    finally:
        connection.close()


def _integer(value):
    require(type(value) is int or isinstance(value, str) and value.isascii() and value.isdigit(),
            'invalid_pagination')
    return int(value)


class ApolloReadOnly:
    def __init__(self, credential_loader=None, *, identity_reader=None, request=None,
                 enabled=False, timeout=15):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30,
                'invalid_timeout')
        self._loader, self._identity = credential_loader, identity_reader
        self._request, self.timeout, self.enabled = request or _https_request, timeout, enabled

    def _call(self, method, path, body=None):
        require(self.enabled is True, 'apollo_disabled')
        require(callable(self._loader), 'configuration_pending')
        require((method == 'GET' and path.startswith('/api/v1/emailer_messages/search?'))
                or (method == 'POST' and path == '/api/v1/emailer_messages/get_content'),
                'apollo_endpoint_blocked')
        try:
            token = self._loader()
            require(isinstance(token, str) and token and not any(c.isspace() for c in token),
                    'credential_unavailable')
            status, raw = self._request(method, path, body,
                {'x-api-key': token, 'Content-Type': 'application/json'}, self.timeout)
            require(type(status) is int and 200 <= status < 300, 'apollo_http_error')
            require(isinstance(raw, bytes) and len(raw) <= 1_000_000, 'invalid_response')
            result = json.loads(raw)
            require(isinstance(result, dict), 'invalid_response')
            return result
        except ExternalEmailError:
            raise
        except Exception:
            raise ExternalEmailError('apollo_read_unavailable') from None

    def search(self, *, page=1, per_page=100):
        require(type(page) is int and 1 <= page <= 500 and type(per_page) is int
                and 1 <= per_page <= 100, 'invalid_pagination')
        return self._call('GET', '/api/v1/emailer_messages/search?' + urlencode({
            'emailer_campaign_ids[]': APOLLO_SEQUENCE, 'page': page, 'per_page': per_page}))

    def content(self, ids):
        require(isinstance(ids, list) and 1 <= len(ids) <= 10 and len(set(ids)) == len(ids)
                and all(identifier(value) for value in ids), 'invalid_content_ids')
        result = self._call('POST', '/api/v1/emailer_messages/get_content',
                            {'ids': ids, 'body_format': 'plain'})
        rows = result.get('emailer_messages')
        require(isinstance(rows, list), 'invalid_content_response')
        seen = set()
        for row in rows:
            require(isinstance(row, Mapping) and row.get('id') in ids and row['id'] not in seen,
                    'invalid_content_response')
            require(row.get('status') == 'completed' and row.get('body_format') == 'plain',
                    'not_completed_plain_email')
            seen.add(row['id'])
        return rows

    def job(self, contacts, *, hubspot=None):
        if self.enabled is not True:
            return {**contract_status(), 'status': 'disabled', 'provider_requests': 0}
        if not callable(self._loader) or not callable(self._identity):
            return {**contract_status(), 'status': 'configuration_pending', 'provider_requests': 0}
        try:
            identity = self._identity()
        except Exception:
            raise ExternalEmailError('apollo_identity_unavailable') from None
        require(isinstance(identity, Mapping) and identity.get('team_id') == APOLLO_TEAM,
                'apollo_team_mismatch')
        require(isinstance(contacts, list) and len(contacts) <= 198, 'invalid_contact_batch')
        by_id, identifiers = {}, set()
        for contact in contacts:
            _, values = contact_emails(contact)
            require(contact['id'] not in by_id, 'duplicate_contact_id')
            statuses = contact.get('contact_campaign_statuses', [])
            require(isinstance(statuses, list) and any(isinstance(row, Mapping) and
                row.get('emailer_campaign_id') == APOLLO_SEQUENCE for row in statuses),
                'contact_campaign_mismatch')
            by_id[contact['id']] = contact
            identifiers.update(values)
        metadata, seen, total, requests = [], set(), None, 0
        for page in range(1, 501):
            response = self.search(page=page)
            requests += 1
            pagination = response.get('pagination')
            require(isinstance(pagination, Mapping), 'invalid_pagination')
            count = _integer(pagination.get('total_entries'))
            pages = _integer(pagination.get('total_pages'))
            require(_integer(pagination.get('page')) == page and
                    _integer(pagination.get('per_page')) == 100 and 0 <= count <= 50000
                    and pages == (count + 99) // 100, 'invalid_pagination')
            total = count if total is None else total
            require(total == count, 'apollo_search_changed')
            rows = response.get('emailer_messages')
            require(isinstance(rows, list) and len(rows) <= 100, 'invalid_search_response')
            for row in rows:
                require(isinstance(row, Mapping) and identifier(row.get('id')) and row['id'] not in seen
                        and row.get('emailer_campaign_id') == APOLLO_SEQUENCE, 'invalid_search_response')
                require(row.get('contact_id') in by_id, 'message_contact_outside_cohort')
                seen.add(row['id'])
                metadata.append(row)
            if page >= pages:
                require(len(metadata) == total, 'incomplete_apollo_search')
                break
        else:
            raise ExternalEmailError('incomplete_apollo_search')
        events, missing = [], []
        for start in range(0, len(metadata), 10):
            batch = {row['id']: row for row in metadata[start:start + 10]}
            contents = self.content(list(batch))
            requests += 1
            missing.extend(sorted(set(batch) - {row['id'] for row in contents}))
            for row in contents:
                meta = batch[row['id']]
                contact = by_id[meta['contact_id']]
                _, aliases = contact_emails(contact)
                recipients = row.get('recipients')
                require(isinstance(recipients, list), 'invalid_recipients')
                matches = {email(item.get('email')) for item in recipients if
                           isinstance(item, Mapping) and item.get('type') == 'to'} & set(aliases)
                require(len(matches) == 1, 'recipient_identity_mismatch')
                require(isinstance(row.get('body'), str) and len(row['body']) <= 200_000
                        and (row.get('subject') is None or isinstance(row['subject'], str)),
                        'invalid_email_content')
                events.append({'source': {'provider': 'apollo', 'account_id': APOLLO_TEAM,
                    'external_campaign_id': APOLLO_SEQUENCE, 'campaign_id': 'BR-APOLLO-01',
                    'provider_id': row['id']}, 'event_type': 'email_sent',
                    'source_contact_id': contact['id'], 'lead_email': next(iter(matches)),
                    'occurred_at': timestamp(row.get('sent_at')),
                    'subject': row['subject'] or '', 'text': row['body']})
        snapshot = hubspot.contact_snapshot(sorted(identifiers)) if hubspot is not None else None
        plans = [plan_contact(contact, snapshot) for contact in contacts] if snapshot is not None else []
        return {'status': 'prepared_read_only', 'provider': 'apollo', 'campaign_id': 'BR-APOLLO-01',
                'team_id': APOLLO_TEAM, 'source_complete': not missing, 'metadata_count': len(metadata),
                'missing_content_ids': missing, 'events': events, 'contact_plans': plans,
                'provider_requests': requests, 'writer_enabled': False, 'writes': 0,
                'email_logged': False, 'email_dedupe': 'unknown'}
